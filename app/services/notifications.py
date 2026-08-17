"""
Purchase notifications for customers and admins.

Before this existed, a successful payment told nobody: no receipt, no in-app
notice, and the admin dashboard's bell icon was static markup wired to nothing.

Three channels, each independently optional so a missing one degrades rather than
breaks:

* in-app  — a row in `billing.notifications` for the buyer and one for admins
* email   — a receipt to the customer over SMTP (reuses the platform's existing
            credentials; falls back to the careers sender if no billing sender
            is configured)
* Telegram— an admin ping, if a bot token and chat id are configured

Every entry point is failure-tolerant by design. `payment_settled` is called
*after* the money is confirmed and the entitlement applied, so nothing it does
may ever propagate an exception back into the payment path — a customer who paid
must stay paid even if the mail server is down.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.database import pg
from app.services import pricing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-app
# ---------------------------------------------------------------------------

def record(
    audience: str,
    type_: str,
    title: str,
    body: Optional[str] = None,
    user_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert an in-app notification. Never raises."""
    if not pg.is_configured():
        return
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO billing.notifications
                    (audience, user_id, type, title, body, meta)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    audience, user_id, type_, title[:300],
                    (body or "")[:2000] or None,
                    json.dumps(meta or {}, ensure_ascii=False)[:4000],
                ),
            )
    except Exception as e:
        logger.warning("Could not record %s notification: %s", audience, e)


def list_for_user(user_id: str, limit: int = 50, unread_only: bool = False) -> list[dict]:
    """A customer's notifications, newest first."""
    return _list("user", user_id, limit, unread_only)


def list_for_admin(limit: int = 50, unread_only: bool = False) -> list[dict]:
    """Admin-wide notifications, newest first."""
    return _list("admin", None, limit, unread_only)


def _list(audience: str, user_id: Optional[str], limit: int, unread_only: bool) -> list[dict]:
    if not pg.is_configured():
        return []
    where = ["audience = %s"]
    params: list[Any] = [audience]
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    if unread_only:
        where.append("read_at IS NULL")
    params.append(max(1, min(limit, 200)))

    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT id, type, title, body, meta, read_at, created_at
            FROM billing.notifications
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "id": r[0], "type": r[1], "title": r[2], "body": r[3], "meta": r[4],
            "read": r[5] is not None,
            "read_at": r[5].isoformat() if r[5] else None,
            "created_at": r[6].isoformat(),
        }
        for r in rows
    ]


def mark_read(notification_id: int, audience: str, user_id: Optional[str]) -> bool:
    """
    Mark one notification read.

    Scoped by audience and user so a caller cannot mark somebody else's
    notifications — the id alone is not authority.
    """
    if not pg.is_configured():
        return False
    where = ["id = %s", "audience = %s"]
    params: list[Any] = [notification_id, audience]
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    with pg.cursor() as cur:
        cur.execute(
            f"UPDATE billing.notifications SET read_at = now() "
            f"WHERE {' AND '.join(where)} AND read_at IS NULL",
            params,
        )
        return cur.rowcount > 0


def unread_count(audience: str, user_id: Optional[str] = None) -> int:
    """How many unread notifications the given audience has."""
    if not pg.is_configured():
        return 0
    where = ["audience = %s", "read_at IS NULL"]
    params: list[Any] = [audience]
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"SELECT count(*) FROM billing.notifications WHERE {' AND '.join(where)}",
            params,
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Outbound channels
# ---------------------------------------------------------------------------

async def _send_email(to_address: str, subject: str, html: str, text: str) -> bool:
    """Send one email over SMTP. Returns False instead of raising."""
    host = settings.smtp_host
    if not host or not to_address:
        return False

    try:
        import aiosmtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = settings.payments_from_email or settings.smtp_from_email
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        await aiosmtplib.send(
            message,
            hostname=host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_port == 587,
            use_tls=settings.smtp_port == 465,
            timeout=20,
        )
        logger.info("Receipt emailed to %s", to_address)
        return True
    except Exception as e:
        logger.warning("Could not email %s: %s", to_address, e)
        return False


async def _telegram_admin(text: str) -> bool:
    """Ping the admin Telegram chat, if configured."""
    token = settings.telegram_bot_token
    chat_id = settings.admin_telegram_chat_id
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            response.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Could not send admin Telegram alert: %s", e)
        return False


# ---------------------------------------------------------------------------
# The event
# ---------------------------------------------------------------------------

def _format_idr(amount: Any) -> str:
    """Rp 1.551.930 — Indonesian thousands separator."""
    try:
        return "Rp " + f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "Rp -"


async def payment_settled(record_: Dict[str, Any], detail: str) -> None:
    """
    Announce a confirmed, granted purchase to the buyer and to admins.

    Called only after settlement is verified and the entitlement applied, so the
    message can state plainly that access is live.
    """
    order_id = record_.get("order_id") or ""
    user_id = record_.get("user_id") or ""
    product = record_.get("product") or ""
    label = pricing.product_name(product)
    amount_usd = float(record_.get("amount") or 0)
    amount_idr = record_.get("amount_idr")
    email = record_.get("customer_email")

    meta = {
        "order_id": order_id,
        "product": product,
        "amount_usd": amount_usd,
        "amount_idr": amount_idr,
        "payment_type": record_.get("payment_type"),
    }

    # In-app: buyer
    record(
        audience="user",
        user_id=user_id,
        type_="payment_settled",
        title=f"Payment confirmed — {label}",
        body=(
            f"We received {_format_idr(amount_idr)} (${amount_usd:.2f}) for {label}. "
            "Your access is active."
        ),
        meta=meta,
    )

    # In-app: admins
    record(
        audience="admin",
        type_="payment_settled",
        title=f"Payment received — {label}",
        body=(
            f"{_format_idr(amount_idr)} (${amount_usd:.2f}) from "
            f"{email or user_id} via {record_.get('payment_type') or 'midtrans'}."
        ),
        meta={**meta, "user_id": user_id, "email": email},
    )

    # Email receipt to the customer
    if email:
        await _send_receipt(email, label, amount_usd, amount_idr, order_id, record_)

    # Admin Telegram ping
    await _telegram_admin(
        f"💰 <b>Payment received</b>\n{label}\n"
        f"{_format_idr(amount_idr)} (${amount_usd:.2f})\n"
        f"{email or user_id}\nOrder: <code>{order_id}</code>"
    )


async def _send_receipt(
    email: str,
    label: str,
    amount_usd: float,
    amount_idr: Any,
    order_id: str,
    record_: Dict[str, Any],
) -> bool:
    """
    Deliver the receipt through the n8n workflow, falling back to SMTP.

    Only structured fields are posted — n8n owns the template, the same split
    the free-assessment report email already uses. Never raises: a receipt that
    cannot be delivered must not unsettle a payment that already succeeded.
    """
    invoice_number = record_.get("invoice_number") or order_id
    receipt_url = settings.payment_finish_redirect_url

    webhook = settings.n8n_receipt_webhook_url
    token = settings.receipt_email_token
    if webhook and token:
        payload = {
            "token": token,
            "email": email,
            "invoice_number": invoice_number,
            "order_id": order_id,
            "product_label": label,
            "payment_method": record_.get("payment_type") or "midtrans",
            "amount_idr_formatted": _format_idr(amount_idr),
            "amount_usd": amount_usd,
            "paid_at": str(record_.get("created_at") or ""),
            "receipt_url": receipt_url,
        }
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(webhook, json=payload)
            if response.status_code == 200:
                logger.info("Receipt queued via n8n for %s (order %s)", email, order_id)
                return True
            # A 4xx here means the workflow rejected our payload — a token
            # mismatch or a malformed field. Log the status so it is diagnosable
            # without dumping the token into the logs.
            logger.warning(
                "n8n rejected the receipt for order %s (HTTP %s); falling back to SMTP",
                order_id, response.status_code,
            )
        except Exception as e:
            logger.warning(
                "Could not reach the n8n receipt workflow for order %s (%s); falling back to SMTP",
                order_id, e,
            )

    return await _send_email(
        to_address=email,
        subject=f"Your Aivory receipt — {label} ({invoice_number})",
        html=_receipt_html(label, amount_usd, amount_idr, order_id, record_, receipt_url),
        text=(
            f"Thank you for your purchase.\n\n"
            f"{label}\n"
            f"Amount: {_format_idr(amount_idr)} (${amount_usd:.2f})\n"
            f"Invoice: {invoice_number}\n"
            f"Order: {order_id}\n\n"
            f"Your access is now active.\n"
            f"Download a PDF receipt: {receipt_url}\n\n"
            f"— Aivory AI"
        ),
    )


def _receipt_html(
    label: str,
    amount_usd: float,
    amount_idr: Any,
    order_id: str,
    record_: Dict[str, Any],
    receipt_url: str = "",
) -> str:
    """
    A self-contained receipt email.

    Inline-styled and table-based on purpose — mail clients strip <style> blocks
    and ignore most modern CSS. It confirms the payment and links to the
    dashboard, where the branded PDF invoice can be downloaded; the PDF is the
    formal document, this is the notification.
    """
    created = record_.get("created_at") or ""
    method = record_.get("payment_type") or "midtrans"
    invoice_number = record_.get("invoice_number") or order_id
    cta = (
        f'<p style="margin:24px 0 0"><a href="{receipt_url}" '
        'style="display:inline-block;background:#b7cba6;color:#111827;'
        'text-decoration:none;padding:10px 18px;border-radius:8px;'
        'font-size:14px;font-weight:600">View or download receipt</a></p>'
        if receipt_url
        else ""
    )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;color:#111827;line-height:1.5">
  <h2 style="margin:0 0 4px;font-size:20px">Payment confirmed</h2>
  <p style="margin:0 0 24px;color:#6b7280;font-size:14px">
    Thank you — your access is now active.
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:8px 0;color:#6b7280">Invoice</td>
        <td style="padding:8px 0;text-align:right;font-family:monospace;font-size:12px">
          {invoice_number}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Item</td>
        <td style="padding:8px 0;text-align:right;font-weight:600">{label}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Amount</td>
        <td style="padding:8px 0;text-align:right;font-weight:600">
          {_format_idr(amount_idr)}
          <span style="color:#6b7280;font-weight:400">(${amount_usd:.2f})</span>
        </td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Method</td>
        <td style="padding:8px 0;text-align:right">{method}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Order</td>
        <td style="padding:8px 0;text-align:right;font-family:monospace;font-size:12px">
          {order_id}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Date</td>
        <td style="padding:8px 0;text-align:right">{created[:19].replace('T', ' ')} UTC</td></tr>
  </table>
  {cta}
  <p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #e5e7eb;
            color:#9ca3af;font-size:12px">
    Aivory AI · This is an automated receipt.
  </p>
</div>"""
