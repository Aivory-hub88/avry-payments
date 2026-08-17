"""
Payment Gateway API Routes
Handles Midtrans payment processing and transaction management.

Authorisation rules
-------------------
* `/midtrans/webhook` is public but authenticated by Midtrans' `signature_key`,
  which is derived from the server key. An unsigned request is rejected.
* Everything else that reads or moves money requires a JWT. User-scoped routes
  additionally check ownership, so one customer cannot read or settle another's
  orders.
* `/config` and `/client-key` stay public: the Snap client key is meant for the
  browser.
* Amounts are never taken from the request body — see `app.services.pricing`.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import require_admin, require_auth
from app.config import settings
from app.database import payment_repo
from app.services import fx, pricing, receipt_pdf
from app.services.payment_gateway import midtrans_service
from app.services.payment_validation import PaymentValidationService
from app.utils.id_generator import generate_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

ADMIN_ACCOUNT_TYPES = {"admin", "superadmin"}


# ============================================================================
# Caller identity helpers
# ============================================================================

def _caller_user_id(payload: dict) -> Optional[str]:
    """The authenticated caller's user id, across token shapes."""
    return payload.get("user_id") or payload.get("sub")


def _is_admin(payload: dict) -> bool:
    """Whether the caller may act across other users' data."""
    account_type = (
        payload.get("account_type")
        or (payload.get("user_metadata") or {}).get("account_type")
        or (payload.get("app_metadata") or {}).get("account_type")
    )
    return account_type in ADMIN_ACCOUNT_TYPES


def _require_self_or_admin(payload: dict, user_id: str) -> None:
    """Reject a caller reaching for someone else's data."""
    if _is_admin(payload):
        return
    if _caller_user_id(payload) != user_id:
        raise HTTPException(status_code=403, detail="Not permitted for this user")


# ============================================================================
# Pydantic Models
# ============================================================================

class CreateTransactionRequest(BaseModel):
    """
    Request to create a new payment transaction.

    `amount` is intentionally absent: the price comes from the server-side
    catalogue. `user_id` is optional and only honoured for admins — everyone
    else transacts as themselves.
    """
    product: str = Field(..., description="Product being purchased")
    user_id: Optional[str] = Field(None, description="Target user (admins only)")
    customer_email: Optional[str] = Field(None, description="Customer email")
    customer_first_name: Optional[str] = Field(None, description="Customer first name")
    custom_field1: Optional[str] = Field(None, description="Custom field 1")
    custom_field2: Optional[str] = Field(None, description="Custom field 2")
    enabled_payments: Optional[List[str]] = Field(
        None,
        description="Restrict the Snap channel list to the one the customer picked. "
                    "Validated against the gateway allowlist; unknown values are ignored.",
    )


class CreateTransactionResponse(BaseModel):
    """Response for transaction creation."""
    success: bool
    order_id: Optional[str] = None
    token: Optional[str] = None
    redirect_url: Optional[str] = None
    transaction_id: Optional[str] = None
    amount_usd: Optional[float] = None
    amount_idr: Optional[int] = None
    error: Optional[str] = None


class TransactionStatusResponse(BaseModel):
    """Response for transaction status check."""
    success: bool
    order_id: Optional[str] = None
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None
    payment_type: Optional[str] = None
    fraud_status: Optional[str] = None
    gross_amount: Optional[str] = None
    error: Optional[str] = None


class RefundRequest(BaseModel):
    """Request to process a refund."""
    order_id: str = Field(..., description="Order ID to refund")
    amount: Optional[int] = Field(None, description="Amount to refund (full if not specified)")


class RefundResponse(BaseModel):
    """Response for refund processing."""
    success: bool
    order_id: Optional[str] = None
    refund_id: Optional[str] = None
    refund_amount: Optional[str] = None
    error: Optional[str] = None


class PaymentHistoryResponse(BaseModel):
    """Response for payment history."""
    success: bool
    payments: list[Dict[str, Any]] = []
    total: int = 0


class ManualPaymentRequest(BaseModel):
    """
    A customer declaring that they paid out-of-band (transfer / cash / e-wallet).

    Nothing here grants anything: the amount is still priced server-side and the
    order lands in `awaiting_verification` until an admin approves it against the
    bank statement. `user_id` is honoured for admins only.
    """
    product: str = Field(..., description="Product being purchased")
    payment_method: str = Field(
        "bank_transfer", description="bank_transfer | cash | ewallet"
    )
    transaction_reference: str = Field(
        ..., min_length=3, max_length=128,
        description="Bank/e-wallet reference the customer supplies as proof",
    )
    customer_email: Optional[str] = Field(None, description="Customer email")
    user_id: Optional[str] = Field(None, description="Target user (admins only)")


class ManualDecisionRequest(BaseModel):
    """An admin's verdict on a manual payment."""
    reason: Optional[str] = Field(None, max_length=500)


class ConfirmPaymentRequest(BaseModel):
    """
    Request to confirm a payment.

    Only the order id is accepted. The product, amount and paid/unpaid state are
    all re-derived server-side — a client that could assert them could grant
    itself anything.
    """
    order_id: str = Field(..., description="Midtrans order ID")


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/midtrans/create", response_model=CreateTransactionResponse)
async def create_midtrans_transaction(
    request: CreateTransactionRequest,
    caller: dict = Depends(require_auth),
):
    """
    Create a Midtrans Snap transaction for the authenticated caller.

    Returns the Snap token and redirect URL for the checkout flow. The charged
    amount is resolved from the server-side catalogue, not from the request.
    """
    # Admins may transact on another user's behalf; everyone else is themselves.
    target_user_id = _caller_user_id(caller)
    if request.user_id and request.user_id != target_user_id:
        if not _is_admin(caller):
            raise HTTPException(status_code=403, detail="Cannot transact for another user")
        target_user_id = request.user_id

    if not target_user_id:
        raise HTTPException(status_code=400, detail="Could not determine the paying user")

    # Price in IDR against a current rate, not a stale one.
    await fx.ensure_fresh()

    try:
        amount_usd = pricing.resolve_price_usd(request.product)
        amount_idr = pricing.resolve_gross_amount_idr(request.product)
    except pricing.UnknownProduct as e:
        raise HTTPException(status_code=400, detail=str(e))

    customer_details: Dict[str, Any] = {}
    if request.customer_email:
        customer_details["email"] = request.customer_email
    if request.customer_first_name:
        customer_details["first_name"] = request.customer_first_name

    result = await midtrans_service.create_transaction(
        user_id=target_user_id,
        product=request.product,
        customer_details=customer_details,
        custom_field1=request.custom_field1,
        custom_field2=request.custom_field2,
        enabled_payments=request.enabled_payments,
    )

    if result.get("success"):
        # Record the order *with the server-side amount and the rate that
        # produced it*, so settlement can later check that Midtrans collected
        # exactly this much, and so an old charge stays explainable.
        payment_repo.create({
            "payment_id": generate_id("payment"),
            "order_id": result["order_id"],
            "user_id": target_user_id,
            "product": pricing.canonical_product(request.product),
            "amount": amount_usd,
            "amount_idr": amount_idr,
            "usd_idr_rate": fx.current_rate(),
            "status": "pending",
            "payment_method": "midtrans",
            "is_mock": result.get("is_mock", False),
            "transaction_id": result.get("transaction_id"),
            "customer_email": request.customer_email,
        })

    return CreateTransactionResponse(**result)


@router.get("/receipt/{order_id}")
async def download_receipt(order_id: str, sig: str = ""):
    """
    Return the receipt PDF for one order.

    Deliberately unauthenticated: this is the link inside the receipt email, and
    requiring a session would send customers to a login screen to read a
    document that is already theirs. The HMAC in `sig` is what authorises it --
    it is per-order and unguessable, so possession of the emailed link is the
    proof, exactly as it is for a password-reset URL.

    A wrong or missing signature is a flat 404 rather than a 403: telling an
    enumerator that an order exists but their signature is wrong is more than
    they need to know.
    """
    if not receipt_pdf.signature_valid(order_id, sig):
        raise HTTPException(status_code=404, detail="Receipt not found")

    record = payment_repo.find_by_order_id(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Receipt not found")

    label = pricing.product_name(record.get("product") or "")
    pdf = receipt_pdf.render(record, label)
    if pdf is None:
        raise HTTPException(status_code=503, detail="Receipt rendering unavailable")

    invoice = record.get("invoice_number") or order_id
    filename = f"Aivory-receipt-{invoice}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/midtrans/status/{order_id}", response_model=TransactionStatusResponse)
async def check_transaction_status(order_id: str, caller: dict = Depends(require_auth)):
    """Check a transaction's status. Callers may only query their own orders."""
    record = _find_payment(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order")
    _require_self_or_admin(caller, record.get("user_id"))

    try:
        result = await midtrans_service.get_transaction_status(order_id)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return TransactionStatusResponse(**{
        k: v for k, v in result.items() if k in TransactionStatusResponse.model_fields
    })


@router.post("/midtrans/webhook")
async def midtrans_webhook(request: Request):
    """
    Receive a Midtrans HTTP notification.

    Authentication is the notification's own `signature_key`
    (SHA512(order_id + status_code + gross_amount + server_key)); only Midtrans
    can produce it. Unsigned or mis-signed calls get a 403 and are never acted on.

    Configure this URL in the Midtrans dashboard under
    Settings -> Configuration -> Payment Notification URL.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON body")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    if not midtrans_service.verify_notification_signature(payload):
        # Deliberately terse: don't help an attacker tune their forgery.
        raise HTTPException(status_code=403, detail="Invalid signature")

    result = await midtrans_service.handle_webhook(payload)

    if not result.get("success"):
        if result.get("permanent"):
            # Retrying will never succeed (unknown order, amount mismatch), so
            # acknowledge to stop Midtrans re-delivering forever.
            #
            # "Unknown order" is routine — Midtrans' dashboard test button sends
            # a `payment_notif_test_*` order that by definition isn't ours — so
            # it stays at WARNING. Anything else here (amount mismatch, failed
            # grant) is already logged at ERROR by settle_order and wants a human.
            logger.warning("Webhook acknowledged without action: %s", result)
            return {"success": False, "acknowledged": True, "reason": result.get("error")}

        # Transient (e.g. identity service down): a non-2xx makes Midtrans retry,
        # which is safe because the grant is idempotent.
        logger.error("Webhook processing failed, asking Midtrans to retry: %s", result)
        raise HTTPException(status_code=503, detail="Could not process notification")

    return result


@router.post("/confirm")
async def confirm_payment(
    request: ConfirmPaymentRequest,
    caller: dict = Depends(require_auth),
):
    """
    Settle an order after the customer finishes Snap checkout.

    This is a convenience path so the dashboard can update immediately instead
    of waiting for the webhook; it is not a source of truth. The status is
    re-fetched from Midtrans and the grant is the same idempotent operation the
    webhook uses, so calling both is harmless.
    """
    record = _find_payment(request.order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order")
    _require_self_or_admin(caller, record.get("user_id"))

    result = await midtrans_service.settle_order(
        request.order_id,
        expected_user_id=None if _is_admin(caller) else _caller_user_id(caller),
    )

    if not result.get("success"):
        return {
            "success": False,
            "message": result.get("error", "Could not confirm payment"),
            "order_id": request.order_id,
        }

    return {
        "success": True,
        "message": "Payment confirmed" if result.get("granted") or result.get("already_granted")
                   else "Payment not yet settled",
        "order_id": request.order_id,
        "status": result.get("status"),
        "granted": bool(result.get("granted") or result.get("already_granted")),
        "detail": result.get("detail"),
    }


# ============================================================================
# Manual (out-of-band) payments
# ============================================================================
#
# Some customers pay by bank transfer rather than through Snap. That path used to
# post to the admin-only `/record` endpoint from the browser, which could only
# ever 401. It now has its own submit/approve pair: the customer files a claim,
# an admin approves it, and approval reuses the same entitlement call the Midtrans
# settlement path uses, so a manually-paid order grants exactly what a card-paid
# one does — once.

MANUAL_METHODS = {"bank_transfer", "cash", "ewallet"}
MANUAL_PENDING_STATUS = "awaiting_verification"


@router.post("/manual/submit")
async def submit_manual_payment(
    request: ManualPaymentRequest,
    caller: dict = Depends(require_auth),
):
    """
    File an out-of-band payment for admin verification.

    Records what the customer *claims*, priced from the catalogue. No entitlement
    is applied here — see `/manual/{order_id}/approve`.
    """
    if request.payment_method not in MANUAL_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"payment_method must be one of {sorted(MANUAL_METHODS)}",
        )

    target_user_id = _caller_user_id(caller)
    if request.user_id and request.user_id != target_user_id:
        if not _is_admin(caller):
            raise HTTPException(status_code=403, detail="Cannot transact for another user")
        target_user_id = request.user_id

    if not target_user_id:
        raise HTTPException(status_code=400, detail="Could not determine the paying user")

    await fx.ensure_fresh()

    try:
        amount_usd = pricing.resolve_price_usd(request.product)
        amount_idr = pricing.resolve_gross_amount_idr(request.product)
    except pricing.UnknownProduct as e:
        raise HTTPException(status_code=400, detail=str(e))

    canonical = pricing.canonical_product(request.product)
    order_id = generate_id(f"manual_{canonical}")

    record = payment_repo.create({
        "payment_id": generate_id("payment"),
        "order_id": order_id,
        "user_id": target_user_id,
        "product": canonical,
        "amount": amount_usd,
        "amount_idr": amount_idr,
        "usd_idr_rate": fx.current_rate(),
        "status": MANUAL_PENDING_STATUS,
        "payment_method": f"manual_{request.payment_method}",
        # The customer's own reference, not a gateway id — kept so an admin can
        # match the claim against the bank statement.
        "transaction_id": request.transaction_reference,
        "customer_email": request.customer_email,
        "is_mock": False,
    })

    logger.info(
        "Manual payment filed: %s (%s, $%s) by %s",
        order_id, canonical, amount_usd, target_user_id,
    )

    return {
        "success": True,
        "order_id": order_id,
        "status": record["status"],
        "product": canonical,
        "amount_usd": amount_usd,
        "amount_idr": amount_idr,
        "message": "Payment submitted for verification",
    }


@router.get("/manual/pending")
async def list_pending_manual_payments(_admin: dict = Depends(require_admin)):
    """Every manual payment still awaiting a decision, oldest first."""
    rows, total = payment_repo.query(
        status=MANUAL_PENDING_STATUS, sort_by="created_at", sort_dir="asc", limit=200
    )
    return {"success": True, "payments": rows, "total": total}


@router.post("/manual/{order_id}/approve")
async def approve_manual_payment(
    order_id: str,
    request: ManualDecisionRequest | None = None,
    admin: dict = Depends(require_admin),
):
    """
    Accept a verified out-of-band payment and apply its entitlement, once.

    The row lock plus the `granted_at` re-check inside it are what make a double
    click (or two admins) grant only once; the grant itself is additionally
    idempotent on order_id in avry-backend.
    """
    from app.services.entitlements import apply_entitlement

    record = _find_payment(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order")
    if not str(record.get("payment_method") or "").startswith("manual_"):
        raise HTTPException(status_code=400, detail="Not a manual payment")

    settled_record = None
    detail = ""
    # Outcomes are collected and acted on *after* the lock closes. Raising inside
    # the block would roll the transaction back, discarding the very status write
    # that records the failure.
    already_granted = False
    conflict: Optional[str] = None
    grant_failed = False

    with payment_repo.SettlementLock(order_id) as lock:
        locked = lock.record
        if not locked:
            conflict = "missing"
        elif locked.get("granted_at"):
            already_granted = True
        elif locked.get("status") not in (MANUAL_PENDING_STATUS, "pending"):
            conflict = f"Order is {locked.get('status')}, not awaiting verification"
        else:
            granted, detail = await apply_entitlement(
                user_id=locked.get("user_id"),
                product=locked.get("product"),
                amount_usd=float(locked.get("amount") or 0),
                order_id=order_id,
            )

            if granted:
                lock.update(
                    status="paid",
                    granted_at=datetime.now(timezone.utc),
                    grant_detail={
                        "detail": detail,
                        "approved_by": _caller_user_id(admin),
                        "reason": (request.reason if request else None),
                    },
                )
                payment_repo.assign_invoice_number(lock)
                settled_record = dict(lock.record or locked)
            else:
                # Committed, not raised: the order must be visibly stuck so an
                # admin can retry it once identity is reachable again.
                lock.update(status="paid_grant_failed", grant_error=detail)
                grant_failed = True

    if conflict == "missing":
        raise HTTPException(status_code=404, detail="Unknown order")
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    if already_granted:
        return {
            "success": True, "order_id": order_id,
            "status": "paid", "already_granted": True,
        }
    if grant_failed:
        logger.error("Manual order %s approved but entitlement failed: %s", order_id, detail)
        raise HTTPException(
            status_code=503,
            detail="Payment approved but entitlement failed; retry once identity is reachable",
        )

    # Receipts are best-effort: a dead mail server must not undo an approval.
    try:
        from app.services import notifications

        await notifications.payment_settled(settled_record, detail)
    except Exception as e:
        logger.error("Notification failed for manual order %s (payment stands): %s", order_id, e)

    logger.info("Manual order %s approved by %s", order_id, _caller_user_id(admin))
    return {
        "success": True, "order_id": order_id, "status": "paid",
        "granted": True, "detail": detail,
    }


@router.post("/manual/{order_id}/reject")
async def reject_manual_payment(
    order_id: str,
    request: ManualDecisionRequest | None = None,
    admin: dict = Depends(require_admin),
):
    """Turn down an unverifiable manual payment claim. Never touches entitlements."""
    record = _find_payment(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order")
    if not str(record.get("payment_method") or "").startswith("manual_"):
        raise HTTPException(status_code=400, detail="Not a manual payment")

    with payment_repo.SettlementLock(order_id) as lock:
        locked = lock.record
        if not locked:
            raise HTTPException(status_code=404, detail="Unknown order")
        if locked.get("granted_at"):
            raise HTTPException(
                status_code=409, detail="Order already granted; refund instead of rejecting"
            )
        lock.update(
            status="rejected",
            grant_error=(request.reason if request and request.reason else "rejected by admin"),
        )

    logger.info("Manual order %s rejected by %s", order_id, _caller_user_id(admin))
    return {"success": True, "order_id": order_id, "status": "rejected"}


@router.post("/midtrans/refund", response_model=RefundResponse)
async def refund_payment(request: RefundRequest, _admin: dict = Depends(require_admin)):
    """Refund a payment. Admin/superadmin only — this moves money outward."""
    try:
        result = await midtrans_service.refund_payment(
            order_id=request.order_id,
            amount=request.amount,
        )
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return RefundResponse(**result)


@router.get("/history/admin", response_model=PaymentHistoryResponse)
async def get_all_payments_admin(_admin: dict = Depends(require_admin)):
    """
    Get all payments across all users (admin endpoint).

    Requires an admin/superadmin caller. Returns every payment transaction
    recorded by the service, sorted by creation time (most recent first).
    This is the source of truth for the admin dashboard payments view.
    """
    rows, total = payment_repo.query(limit=500)
    return PaymentHistoryResponse(success=True, payments=rows, total=total)


@router.get("/history/{user_id}", response_model=PaymentHistoryResponse)
async def get_payment_history(user_id: str, caller: dict = Depends(require_auth)):
    """Payment history for a user. Callers may only read their own."""
    _require_self_or_admin(caller, user_id)

    rows, total = payment_repo.query(user_id=user_id, limit=200)
    return PaymentHistoryResponse(success=True, payments=rows, total=total)


@router.post("/record")
async def record_payment(
    user_id: str,
    amount: float,
    payment_method: str = "manual",
    product: str = "ai_blueprint",
    _admin: dict = Depends(require_admin),
):
    """
    Record a manual payment (admin endpoint).

    Requires an admin/superadmin caller. This endpoint allows admins to
    manually record payments that were made outside of the Midtrans system
    (e.g., bank transfer, cash).
    """
    payment_service = PaymentValidationService()
    success = await payment_service.record_payment(
        user_id=user_id,
        amount=amount,
        payment_method=payment_method,
        product=product,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to record payment")

    return {
        "success": True,
        "message": "Payment recorded successfully",
        "user_id": user_id,
        "amount": amount,
        "product": product,
    }


@router.get("/client-key")
async def get_client_key():
    """
    Get Midtrans client key for frontend.

    Public by design: the Snap client key is embedded in the browser.
    """
    client_key = midtrans_service.get_client_key()

    if not client_key:
        raise HTTPException(status_code=503, detail="Midtrans client key not configured")

    return {
        "success": True,
        "client_key": client_key,
        "is_production": midtrans_service.is_production,
        "snap_js_url": midtrans_service.snap_js_url,
    }


@router.get("/config")
async def get_payment_config():
    """
    Payment configuration for the frontend.

    `snap_js_url` matters: loading the sandbox Snap bundle against a production
    token (or vice versa) fails at checkout, so the frontend must take the URL
    from here rather than hardcoding one.
    """
    # Quote the same rate the charge will use.
    await fx.ensure_fresh()

    is_configured = midtrans_service.is_configured()
    return {
        "success": True,
        "is_configured": is_configured,
        "is_mock": midtrans_service.mock_mode,
        "is_production": midtrans_service.is_production,
        "client_key": midtrans_service.get_client_key(),
        "snap_js_url": midtrans_service.snap_js_url,
        "currency": "IDR",
        "fx": fx.rate_info(),
        "usd_idr_rate": fx.current_rate(),
        "products": pricing.public_catalogue(),
    }


# ============================================================================
# Admin listing
# ============================================================================

@router.get("")
async def list_payments(_admin: dict = Depends(require_admin)):
    """List all payments (GET /api/v1/payments) - admin only."""
    rows, total = payment_repo.query(limit=500)
    return {
        "success": True,
        "payments": rows,
        "total": total,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Internals
# ============================================================================

def _find_payment(order_id: str) -> Optional[Dict[str, Any]]:
    """Look up a payment record by Midtrans order id."""
    return payment_repo.find_by_order_id(order_id)
