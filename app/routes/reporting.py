"""
Admin reporting: the payment ledger and the FX rate series, as JSON or CSV.

Filtering, sorting and paging happen in Postgres rather than the browser, so
these stay usable as the ledger grows. CSV is streamed from the same query the
JSON view uses, so an export can never disagree with what the page showed.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth import require_admin, require_auth
from app.database import payment_repo
from app.services import fx, notifications

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments", tags=["reporting"])

# An export has to be bounded, or one click can try to serialise the whole table.
EXPORT_MAX_ROWS = 50_000


def _csv_response(filename: str, header: list[str], rows) -> StreamingResponse:
    """Stream rows as CSV without buffering the whole file in memory."""

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in rows:
            writer.writerow(row)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}-{stamp}.csv"'},
    )


# ---------------------------------------------------------------------------
# Payment ledger
# ---------------------------------------------------------------------------

@router.get("/admin/orders")
async def list_orders(
    _admin: dict = Depends(require_admin),
    status: Optional[str] = None,
    product: Optional[str] = None,
    user_id: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_mock: bool = True,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    """The payment ledger, filtered/sorted/paged for the admin table."""
    rows, total = payment_repo.query(
        user_id=user_id, status=status, product=product, search=search,
        date_from=date_from, date_to=date_to, include_mock=include_mock,
        sort_by=sort_by, sort_dir=sort_dir,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return {
        "success": True,
        "payments": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "sortable": sorted(payment_repo.SORTABLE.keys()),
    }


@router.get("/admin/summary")
async def orders_summary(
    _admin: dict = Depends(require_admin),
    include_mock: bool = False,
):
    """Headline totals for the admin dashboard cards."""
    return {"success": True, **payment_repo.summary(include_mock=include_mock)}


@router.get("/admin/orders/export")
async def export_orders(
    _admin: dict = Depends(require_admin),
    status: Optional[str] = None,
    product: Optional[str] = None,
    user_id: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_mock: bool = True,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
):
    """
    The payment list as CSV, honouring the same filters as the table.

    Exporting exactly what the current view shows is the point: a report that
    silently ignores the active filter is worse than no report.
    """
    rows, total = payment_repo.query(
        user_id=user_id, status=status, product=product, search=search,
        date_from=date_from, date_to=date_to, include_mock=include_mock,
        sort_by=sort_by, sort_dir=sort_dir,
        limit=EXPORT_MAX_ROWS, offset=0,
    )
    if total > EXPORT_MAX_ROWS:
        logger.warning(
            "Payment export truncated: %s rows matched, %s exported",
            total, EXPORT_MAX_ROWS,
        )

    header = [
        "created_at", "order_id", "payment_id", "user_id", "customer_email",
        "product", "amount_usd", "amount_idr", "usd_idr_rate", "status",
        "payment_method", "payment_type", "transaction_status", "fraud_status",
        "is_mock", "granted_at",
    ]

    def generate():
        for r in rows:
            yield [
                r.get("created_at"), r.get("order_id"), r.get("payment_id"),
                r.get("user_id"), r.get("customer_email"), r.get("product"),
                f"{r.get('amount', 0):.2f}", r.get("amount_idr"), r.get("usd_idr_rate"),
                r.get("status"), r.get("payment_method"), r.get("payment_type"),
                r.get("transaction_status"), r.get("fraud_status"),
                "yes" if r.get("is_mock") else "no", r.get("granted_at"),
            ]

    return _csv_response("aivory-payments", header, generate())


# ---------------------------------------------------------------------------
# Invoice (customer-facing)
# ---------------------------------------------------------------------------

# Seller details printed on the invoice. Kept here rather than in the frontend
# so an invoice reprinted next year still shows what was issued.
ISSUER = {
    "name": "Aivory AI",
    "legal_name": "Aivory AI",
    "email": "billing@aivory.uk",
    "website": "aivory.id",
}


@router.get("/orders/{order_id}/invoice")
async def get_invoice(order_id: str, caller: dict = Depends(require_auth)):
    """
    Invoice data for one order. A customer may only fetch their own.

    Only settled orders carry an invoice number; a pending order still returns
    its details so the UI can show the order without pretending it is an invoice.
    """
    from app.database import payment_repo
    from app.services import pricing

    record = payment_repo.find_by_order_id(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order")

    caller_id = _caller_id(caller)
    account_type = (
        caller.get("account_type")
        or (caller.get("user_metadata") or {}).get("account_type")
        or (caller.get("app_metadata") or {}).get("account_type")
    )
    if account_type not in ("admin", "superadmin") and record.get("user_id") != caller_id:
        raise HTTPException(status_code=403, detail="Not permitted for this order")

    product = record.get("product") or ""
    return {
        "success": True,
        "issuer": ISSUER,
        "invoice_number": record.get("invoice_number"),
        "is_paid": record.get("status") == "paid",
        "order": {
            "order_id": record.get("order_id"),
            "payment_id": record.get("payment_id"),
            "user_id": record.get("user_id"),
            "customer_email": record.get("customer_email"),
            "product": product,
            "product_name": pricing.product_name(product),
            "amount_usd": record.get("amount"),
            "amount_idr": record.get("amount_idr"),
            "usd_idr_rate": record.get("usd_idr_rate"),
            "status": record.get("status"),
            "payment_method": record.get("payment_method"),
            "payment_type": record.get("payment_type"),
            "created_at": record.get("created_at"),
            "paid_at": record.get("granted_at"),
        },
    }


# ---------------------------------------------------------------------------
# FX rate series
# ---------------------------------------------------------------------------

@router.get("/fx/current")
async def fx_current():
    """The rate in force right now, and how fresh it is."""
    await fx.ensure_fresh()
    return {"success": True, **fx.rate_info()}


@router.get("/fx/history")
async def fx_history(
    _admin: dict = Depends(require_admin),
    days: int = Query(30, ge=1, le=365),
):
    """
    Recorded USD/IDR observations for the admin graph.

    Returned oldest-first because that is the order a chart wants to plot.
    """
    rows = fx.history(days=days)
    rows.reverse()
    return {"success": True, "days": days, "points": rows, "total": len(rows)}


@router.get("/fx/history/export")
async def fx_history_export(
    _admin: dict = Depends(require_admin),
    days: int = Query(90, ge=1, le=365),
):
    """The FX series as CSV, for the currency-movement report."""
    rows = fx.history(days=days, limit=EXPORT_MAX_ROWS)
    rows.reverse()
    header = [
        "fetched_at", "market_rate", "billing_rate", "margin_percent",
        "provider", "provider_updated_at",
    ]

    def generate():
        for r in rows:
            yield [
                r["fetched_at"], r["market_rate"], r["billing_rate"],
                r["margin_percent"], r["provider"], r["provider_updated_at"],
            ]

    return _csv_response("aivory-fx-usd-idr", header, generate())


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _caller_id(payload: dict) -> Optional[str]:
    return payload.get("user_id") or payload.get("sub")


@router.get("/notifications")
async def my_notifications(
    caller: dict = Depends(require_auth),
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = False,
):
    """The caller's own notifications."""
    user_id = _caller_id(caller)
    if not user_id:
        raise HTTPException(status_code=400, detail="Could not identify caller")
    return {
        "success": True,
        "notifications": notifications.list_for_user(user_id, limit, unread_only),
        "unread": notifications.unread_count("user", user_id),
    }


@router.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: int, caller: dict = Depends(require_auth)):
    """Mark one of the caller's notifications read."""
    user_id = _caller_id(caller)
    if not user_id:
        raise HTTPException(status_code=400, detail="Could not identify caller")
    return {"success": notifications.mark_read(notification_id, "user", user_id)}


@router.get("/admin/notifications")
async def admin_notifications(
    _admin: dict = Depends(require_admin),
    limit: int = Query(50, ge=1, le=200),
    unread_only: bool = False,
):
    """Admin-wide notifications, for the dashboard bell."""
    return {
        "success": True,
        "notifications": notifications.list_for_admin(limit, unread_only),
        "unread": notifications.unread_count("admin"),
    }


@router.post("/admin/notifications/{notification_id}/read")
async def admin_read_notification(
    notification_id: int, _admin: dict = Depends(require_admin)
):
    """Mark an admin notification read."""
    return {"success": notifications.mark_read(notification_id, "admin", None)}
