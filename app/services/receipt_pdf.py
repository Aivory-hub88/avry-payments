"""
Downloadable PDF receipts.

The receipt email used to send customers to the dashboard's payments page,
which meant a logged-in session just to look at a document that is already
theirs. This module renders the receipt itself and signs a link to it, so the
button in the email downloads the PDF directly.

The link carries an HMAC of the order id rather than a session: receipts are
per-order, the signature is unguessable, and it stays valid for as long as the
platform secret does — which is what a customer filing an old invoice needs.
"""

import hmac
import hashlib
import logging
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Long enough that guessing is hopeless, short enough to keep the URL readable.
_SIG_LENGTH = 32


def _signing_secret() -> Optional[bytes]:
    """
    Key for the receipt signature.

    `internal_service_token` is the platform's server-to-server secret: it is
    never handed to a browser, which is exactly what this needs. JWT_SECRET is
    passed to this container but is not declared on Settings, so reading it
    here would raise rather than sign.
    """
    secret = settings.internal_service_token or settings.receipt_email_token
    return secret.encode() if secret else None


def signature_for(order_id: str) -> str:
    """
    Deterministic per-order signature. No storage, no expiry to sweep.

    Returns "" when no secret is configured, and `signature_valid` rejects an
    empty signature — so a misconfigured deployment serves no receipts rather
    than serving them to anyone who guesses an order id.
    """
    secret = _signing_secret()
    if not secret:
        logger.error("No signing secret available; receipt links disabled")
        return ""
    return hmac.new(secret, order_id.encode(), hashlib.sha256).hexdigest()[:_SIG_LENGTH]


def signature_valid(order_id: str, provided: str) -> bool:
    """Constant-time comparison — a timing oracle here would leak the signature."""
    expected = signature_for(order_id)
    if not provided or not expected:
        return False
    return hmac.compare_digest(expected, provided)


def download_url_for(order_id: str) -> str:
    """
    Absolute, signed URL for the receipt PDF of `order_id`, or "" when the
    service cannot sign — callers treat an empty URL as "no receipt button"
    rather than emitting a link that would 404 on arrival.
    """
    sig = signature_for(order_id)
    if not sig:
        return ""
    base = settings.payments_public_base_url.rstrip("/")
    return f"{base}/api/v1/payments/receipt/{order_id}?sig={sig}"


def _format_idr(amount: Any) -> str:
    try:
        return "Rp " + f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return ""


def render(record: Dict[str, Any], product_label: str) -> Optional[bytes]:
    """
    One-page A4 receipt. Returns None if the PDF library is unavailable, so a
    missing dependency degrades to "no download" rather than a 500 on a page a
    paying customer reached from their receipt email.

    Typography is Helvetica rather than the brand's Manrope: fpdf2 needs a TTF
    on disk to embed a font, and shipping one into this service for a document
    this plain is not worth the image size.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        logger.warning("fpdf2 is not installed; receipt PDF unavailable")
        return None

    order_id = str(record.get("order_id") or "")
    invoice = str(record.get("invoice_number") or order_id)
    amount_usd = float(record.get("amount") or 0)
    amount_idr = record.get("amount_idr")
    method = str(record.get("payment_type") or "midtrans")
    email = str(record.get("customer_email") or "")
    created = str(record.get("created_at") or "")[:19].replace("T", " ")

    INK = (17, 17, 17)
    MUTED = (138, 138, 133)
    RULE = (220, 220, 215)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)

    # Wordmark. Letterspacing is faked with spaces — fpdf2 has no tracking control.
    pdf.set_xy(20, 22)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*INK)
    pdf.cell(0, 8, "A I V O R Y", align="L")

    pdf.set_xy(20, 36)
    pdf.set_font("Helvetica", "", 22)
    pdf.cell(0, 10, "Payment receipt", align="L")

    pdf.set_xy(20, 47)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, "Thank you - your access is active.", align="L")

    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    pdf.line(20, 58, 190, 58)

    # Amount, given the weight it deserves on a receipt.
    pdf.set_xy(20, 66)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*INK)
    idr = _format_idr(amount_idr)
    pdf.cell(0, 10, idr if idr else f"US$ {amount_usd:,.2f}", align="L")
    if idr:
        pdf.set_xy(20, 77)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, f"US$ {amount_usd:,.2f}", align="L")

    rows = [
        ("Invoice", invoice),
        ("Item", product_label),
        ("Method", method),
        ("Order", order_id),
        ("Date", f"{created} UTC" if created else ""),
        ("Billed to", email),
    ]

    y = 95
    for key, value in rows:
        if not value:
            continue
        pdf.set_xy(20, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(40, 6, key, align="L")
        pdf.set_xy(60, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*INK)
        pdf.cell(130, 6, str(value), align="L")
        y += 9

    pdf.line(20, y + 6, 190, y + 6)
    pdf.set_xy(20, y + 11)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(
        0, 5,
        "Aivory AI - automated receipt. Questions: hello@aivory.uk",
        align="L",
    )

    out = pdf.output()
    return bytes(out) if out is not None else None
