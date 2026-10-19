"""
Purchase notifications for customers and admins.

Before this existed, a successful payment told nobody: no receipt, no in-app
notice, and the admin dashboard's bell icon was static markup wired to nothing.

Four channels, each independently optional so a missing one degrades rather than
breaks:

* in-app  — a row in `billing.notifications` for the buyer and one for admins
* email   — a receipt to the customer over SMTP (reuses the platform's existing
            credentials; falls back to the careers sender if no billing sender
            is configured)
* admin email — a fixed-recipient alert to the people who need to know money
            moved, over the same SMTP path as the receipt
* Telegram— an admin ping, if a bot token and chat id are configured

Every entry point is failure-tolerant by design. `payment_settled` is called
*after* the money is confirmed and the entitlement applied, so nothing it does
may ever propagate an exception back into the payment path — a customer who paid
must stay paid even if the mail server is down.
"""

from __future__ import annotations

import asyncio
import html as html_lib
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


# A small, fixed distribution list rather than a config knob deliberately:
# these are the specific people who need to know money moved, not a setting
# ops would ever change without also updating this file.
ADMIN_ALERT_EMAILS = [
    "billing@aivory.uk",
    "irfan.reichmann@aivory.uk",
    "haswin.bachtiar@aivory.id",
    "clement.hansel@aivory.id",
]


async def _admin_alert_email(
    label: str,
    amount_usd: float,
    amount_idr: Any,
    order_id: str,
    user_id: str,
    email: Optional[str],
    payment_type: Optional[str],
    promo: Optional[Dict[str, Any]] = None,
) -> None:
    """Email the fixed admin list that a payment came in. Never raises."""
    subject = f"Payment received — {label} ({_format_idr(amount_idr)})"
    if promo:
        subject += f" · promo {promo['code']}"
    method = payment_type or "midtrans"
    user_id_display = user_id or "unknown user"
    email_display = email or "no email on file"
    text = (
        f"Aivory received a payment.\n\n"
        f"Item: {label}\n"
        f"Amount: {_format_idr(amount_idr)} (${amount_usd:.2f})\n"
        f"User ID: {user_id_display}\n"
        f"Email: {email_display}\n"
        f"Method: {method}\n"
        f"Order: {order_id}\n"
    )
    promo_rows = ""
    if promo:
        text += "\n" + "\n".join(_promo_lines(promo)) + "\n"
        promo_rows = "".join(
            f'<tr><td style="padding:6px 0;color:#6b7280">{html_lib.escape(k)}</td>'
            f'<td style="padding:6px 0;text-align:right">{html_lib.escape(v)}</td></tr>'
            for k, v in _promo_pairs(promo)
        )
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;color:#111827;line-height:1.5">
  <h2 style="margin:0 0 16px;font-size:18px">Payment received</h2>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:6px 0;color:#6b7280">Item</td>
        <td style="padding:6px 0;text-align:right;font-weight:600">{label}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280">Amount</td>
        <td style="padding:6px 0;text-align:right;font-weight:600">
          {_format_idr(amount_idr)}
          <span style="color:#6b7280;font-weight:400">(${amount_usd:.2f})</span>
        </td></tr>
    <tr><td style="padding:6px 0;color:#6b7280">User ID</td>
        <td style="padding:6px 0;text-align:right;font-family:monospace;font-size:12px">
          {user_id_display}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280">Email</td>
        <td style="padding:6px 0;text-align:right">{email_display}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280">Method</td>
        <td style="padding:6px 0;text-align:right">{method}</td></tr>
    <tr><td style="padding:6px 0;color:#6b7280">Order</td>
        <td style="padding:6px 0;text-align:right;font-family:monospace;font-size:12px">
          {order_id}</td></tr>
    {promo_rows}
  </table>
  <p style="margin:20px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;
            color:#9ca3af;font-size:12px">
    Aivory AI · automated payment alert.
  </p>
</div>"""
    await asyncio.gather(
        *(_send_email(addr, subject, html, text) for addr in ADMIN_ALERT_EMAILS)
    )


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
# Promo conversions
# ---------------------------------------------------------------------------

def _promo_snapshot(record_: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The promo facts for a settled order, or None when it used no code. Adds
    this order's discount to the code's running totals. Never raises: a
    reporting failure must not touch a payment that already succeeded.
    """
    code = record_.get("promo_code")
    if not code:
        return None
    try:
        from app.database import promo_repo

        snap = promo_repo.conversion_snapshot(code)
    except Exception as e:
        logger.warning("Could not compute promo conversion for %s: %s", code, e)
        snap = None
    snap = snap or {"code": code, "customers": None, "max_customers": None, "affiliate": None}
    snap["discount_usd"] = float(record_.get("discount_usd") or 0)
    return snap


def _customers_label(promo: Dict[str, Any]) -> str:
    """"7 customers" / "7 of 20 customers (13 left)"."""
    n = promo.get("customers")
    if n is None:
        return "unknown"
    cap = promo.get("max_customers")
    if cap:
        return f"{n} of {cap} customers ({max(cap - n, 0)} left)"
    return f"{n} customer{'s' if n != 1 else ''}"


def _promo_pairs(promo: Dict[str, Any]) -> list:
    pairs = [
        ("Promo code", f"{promo['code']} (−${promo.get('discount_usd', 0):.2f})"),
        ("Code used by", _customers_label(promo)),
    ]
    aff = promo.get("affiliate")
    if aff:
        pairs.append((
            "Affiliate",
            f"{aff['name']}: {aff['customers']} paying customer"
            f"{'s' if aff['customers'] != 1 else ''}, ${aff['revenue_usd']:,.2f} "
            f"across {aff['codes']} code{'s' if aff['codes'] != 1 else ''}",
        ))
    return pairs


async def _partner_conversion_email(promo: Dict[str, Any], label: str) -> int:
    """
    Tell the distributor partner a code of theirs just converted. Only codes
    linked to a partner account (affiliate_partner_id) are reported, and only
    to that distributor's active partner logins. Carries no buyer identity —
    the code, the product, and the running customer counts. Returns how many
    emails were sent; never raises.
    """
    partner_id = promo.get("affiliate_partner_id")
    if not partner_id:
        return 0
    try:
        from app.database import promo_repo

        recipients = promo_repo.partner_emails(partner_id)
    except Exception as e:
        logger.warning("Could not look up partner emails for %s: %s", partner_id, e)
        return 0
    if not recipients:
        return 0

    code = promo["code"]
    aff = promo.get("affiliate") or {}
    used = _customers_label(promo)
    total = aff.get("customers")
    total_line = (
        f"Across all {aff.get('codes')} of your codes: {total} paying customer"
        f"{'s' if total != 1 else ''}."
        if total is not None and aff.get("codes")
        else ""
    )
    subject = f"Your Aivory code {code} was just used"
    text = (
        f"Good news — a customer just bought {label} with your promo code {code}.\n\n"
        f"{code} has now been used by {used}.\n"
        + (f"{total_line}\n" if total_line else "")
        + "\nThis is an automated message from Aivory."
    )
    esc = html_lib.escape
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;color:#111827;line-height:1.5">
  <h2 style="margin:0 0 12px;font-size:18px">Your code {esc(code)} was just used</h2>
  <p style="margin:0 0 16px">A customer just bought <strong>{esc(label)}</strong>
     with your promo code <strong style="font-family:monospace">{esc(code)}</strong>.</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:6px 0;color:#6b7280">This code</td>
        <td style="padding:6px 0;text-align:right;font-weight:600">{esc(used)}</td></tr>
    {"" if not total_line else f'<tr><td style="padding:6px 0;color:#6b7280">All your codes</td><td style="padding:6px 0;text-align:right;font-weight:600">{total} paying customer{"s" if total != 1 else ""}</td></tr>'}
  </table>
  <p style="margin:20px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;
            color:#9ca3af;font-size:12px">
    Aivory AI · automated partner update.
  </p>
</div>"""
    results = await asyncio.gather(*(_send_email(addr, subject, html, text) for addr in recipients))
    return sum(1 for ok in results if ok)


def _promo_lines(promo: Dict[str, Any]) -> list:
    return [f"{k}: {v}" for k, v in _promo_pairs(promo)]


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
    promo = _promo_snapshot(record_)

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

    # In-app: admins. The dashboard bell renders title + body as plain text
    # (it doesn't surface `meta`), so every fact an admin needs — who bought
    # it, what they bought, how much — has to be spelled out in body itself
    # rather than left in meta for a UI that never reads it.
    record(
        audience="admin",
        type_="payment_settled",
        title=f"Payment received — {label}",
        body=(
            f"{label} — {_format_idr(amount_idr)} (${amount_usd:.2f})  ·  "
            f"Buyer: {user_id or 'unknown user'} ({email or 'no email on file'})  ·  "
            f"Order {order_id} via {record_.get('payment_type') or 'midtrans'}"
        ),
        meta={**meta, "user_id": user_id, "email": email},
    )

    # In-app: admins, a promo conversion. Its own notification so the bell
    # shows each affiliate conversion and the running customer count without
    # opening the promo page.
    if promo:
        aff = promo.get("affiliate")
        record(
            audience="admin",
            type_="promo_conversion",
            title=f"Promo conversion — {promo['code']}"
            + (f" ({aff['name']})" if aff else ""),
            body="  ·  ".join(
                [f"{email or user_id or 'A customer'} bought {label} for ${amount_usd:.2f}"]
                + [f"{k}: {v}" for k, v in _promo_pairs(promo)]
            ),
            meta={
                **meta,
                "user_id": user_id,
                "email": email,
                "promo_code": promo["code"],
                "promo_customers": promo.get("customers"),
                "promo_max_customers": promo.get("max_customers"),
                "affiliate": aff,
                "affiliate_partner_id": promo.get("affiliate_partner_id"),
            },
        )

    # Email receipt to the customer
    if email:
        await _send_receipt(email, label, amount_usd, amount_idr, order_id, record_)

    # Admin email alert — independent of the receipt above and of Telegram,
    # so one channel being down doesn't cost the others.
    await _admin_alert_email(
        label, amount_usd, amount_idr, order_id,
        user_id=user_id,
        email=email,
        payment_type=record_.get("payment_type"),
        promo=promo,
    )

    # The distributor partner whose code this was, if it is linked to one.
    if promo:
        await _partner_conversion_email(promo, label)

    # Admin Telegram ping
    promo_text = ""
    if promo:
        # Telegram parses HTML; an affiliate named "A & B" must not break it.
        promo_text = "\n🎟 " + "\n".join(html_lib.escape(l) for l in _promo_lines(promo))
    await _telegram_admin(
        f"💰 <b>Payment received</b>\n{label}\n"
        f"{_format_idr(amount_idr)} (${amount_usd:.2f})\n"
        f"User: {user_id or 'unknown user'}\n"
        f"Email: {email or 'no email on file'}\n"
        f"Order: <code>{order_id}</code>"
        f"{promo_text}"
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
    # Link straight at the PDF instead of the dashboard's payments page: the
    # customer wanted their receipt, not a login screen in front of it.
    from app.services import receipt_pdf

    receipt_url = receipt_pdf.download_url_for(order_id)

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
