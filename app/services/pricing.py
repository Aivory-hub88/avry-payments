"""
Server-side product catalogue — the single source of truth for what things cost.

Prices MUST NOT be taken from the client. A caller that can name its own price
can buy the Enterprise plan for Rp 1.000, so `resolve_price_usd()` is the only
sanctioned way to turn a product id into an amount, and every Midtrans
transaction is built from its result.

Amounts are authored in USD (the pricing page's currency) and converted to IDR
at `settings.usd_idr_rate` because Midtrans settles in IDR only.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)


class UnknownProduct(ValueError):
    """Raised when a product id is not sellable."""


# One-off purchases and subscription plans, in USD.
# Mirrors the pricing page (foundation $20, acceleration/pro $44,
# intelligence/enterprise $499).
FIXED_PRICES_USD: Dict[str, int] = {
    "ai_snapshot": 29,
    "ai_blueprint": 85,
    # Snapshot + Blueprint together, $15 cheaper than buying both. The dashboard
    # and the settings modal have advertised this bundle since launch; it was not
    # sellable, so every click on it 400'd.
    "ai_fullstack": 99,
    "foundation": 20,
    "acceleration": 44,
    "intelligence": 499,
}

# Credit top-up packs, in USD, keyed by the credits granted.
#
# The four packs the dashboard has always advertised (100/$10, 500/$45,
# 1000/$85, 5000/$400) are the anchors; the rest sit on the same declining
# per-credit curve ($0.11 down to $0.075).
CREDIT_PACKS_USD: Dict[int, int] = {
    50: 6,        # $0.120/credit
    100: 10,      # $0.100/credit  (anchor)
    250: 24,      # $0.096/credit
    500: 45,      # $0.090/credit  (anchor)
    1000: 85,     # $0.085/credit  (anchor)
    2500: 205,    # $0.082/credit
    5000: 400,    # $0.080/credit  (anchor)
    10000: 750,   # $0.075/credit
}

# Legacy/alternate ids the frontend has used for the same products.
ALIASES: Dict[str, str] = {
    "pro": "acceleration",
    "enterprise": "intelligence",
    # The dashboard's feature cards call the deep diagnostic "ai_diagnostic" and
    # the bundle "ai_bundle"; both name products that already exist here.
    "ai_diagnostic": "ai_snapshot",
    "ai_bundle": "ai_fullstack",
    # The marketing site's canonical id for the same bundle.
    "full_stack": "ai_fullstack",
}

# Bounds on free-form wallet top-ups (`wallet_topup_<usd>`).
WALLET_TOPUP_MIN_USD = 5
WALLET_TOPUP_MAX_USD = 1000

# Midtrans rejects transactions under Rp 1.000.
MIN_GROSS_AMOUNT_IDR = 1000

PRODUCT_NAMES: Dict[str, str] = {
    "ai_snapshot": "AI Snapshot",
    "ai_blueprint": "AI System Blueprint",
    "ai_fullstack": "Full Stack Bundle",
    "foundation": "Foundation Plan",
    "acceleration": "Acceleration Plan",
    "intelligence": "Intelligence Plan",
}


def canonical_product(product: str) -> str:
    """Map a caller-supplied product id onto its canonical form."""
    return ALIASES.get(product, product)


def resolve_price_usd(product: str) -> float:
    """
    Return the authoritative USD price for `product`.

    Raises UnknownProduct if the id is not something we sell — callers should
    turn that into a 400 rather than falling back to a client-supplied amount.
    """
    key = canonical_product(product)

    if key in FIXED_PRICES_USD:
        return float(FIXED_PRICES_USD[key])

    if key.startswith("credits_"):
        try:
            credits = int(key.split("_", 1)[1])
        except (IndexError, ValueError):
            raise UnknownProduct(f"malformed credit product: {product}")
        if credits not in CREDIT_PACKS_USD:
            raise UnknownProduct(f"no such credit pack: {product}")
        return float(CREDIT_PACKS_USD[credits])

    if key.startswith("wallet_topup_"):
        # The top-up amount *is* the product, so parse it from the id rather
        # than trusting a separate `amount` field — that keeps the sum charged
        # and the sum credited identical by construction.
        try:
            usd = float(key.split("_", 2)[2])
        except (IndexError, ValueError):
            raise UnknownProduct(f"malformed wallet top-up: {product}")
        if not (WALLET_TOPUP_MIN_USD <= usd <= WALLET_TOPUP_MAX_USD):
            raise UnknownProduct(
                f"wallet top-up must be between ${WALLET_TOPUP_MIN_USD} and "
                f"${WALLET_TOPUP_MAX_USD} (got ${usd})"
            )
        return usd

    raise UnknownProduct(f"unknown product: {product}")


def usd_to_idr(usd_amount: float) -> int:
    """
    Convert USD to whole IDR at the current billing rate.

    Reads the cached live rate (see app/services/fx.py); callers on the request
    path should `await fx.ensure_fresh()` first so the cache is current.
    """
    from app.services import fx

    return int(round(usd_amount * fx.current_rate()))


def resolve_gross_amount_idr(product: str) -> int:
    """Return the IDR amount to charge for `product`, validated against Midtrans' floor."""
    idr = usd_to_idr(resolve_price_usd(product))
    if idr < MIN_GROSS_AMOUNT_IDR:
        raise UnknownProduct(
            f"{product} converts to Rp {idr}, below Midtrans' Rp {MIN_GROSS_AMOUNT_IDR} minimum"
        )
    return idr


def product_name(product: str) -> str:
    """Human-readable label for a product id, used in Midtrans item details."""
    key = canonical_product(product)
    if key in PRODUCT_NAMES:
        return PRODUCT_NAMES[key]
    if key.startswith("credits_"):
        return f"{key.split('_', 1)[1]} Credits"
    if key.startswith("wallet_topup_"):
        return f"Wallet Top-up ${key.split('_', 2)[2]}"
    return key


def is_sellable(product: str) -> bool:
    """Whether `product` has an authoritative price."""
    try:
        resolve_price_usd(product)
        return True
    except UnknownProduct:
        return False


def public_catalogue() -> Dict[str, Dict[str, object]]:
    """The catalogue as the frontend needs it: id -> name/USD/IDR."""
    catalogue: Dict[str, Dict[str, object]] = {}
    for key in FIXED_PRICES_USD:
        usd = float(FIXED_PRICES_USD[key])
        catalogue[key] = {
            "name": product_name(key),
            "price_usd": usd,
            "price_idr": usd_to_idr(usd),
        }
    for credits, usd in CREDIT_PACKS_USD.items():
        key = f"credits_{credits}"
        catalogue[key] = {
            "name": product_name(key),
            "price_usd": float(usd),
            "price_idr": usd_to_idr(usd),
            "credits": credits,
        }
    return catalogue


def credits_for_product(product: str) -> Optional[int]:
    """Credits granted by `product`, or None if it isn't a credit pack."""
    key = canonical_product(product)
    if not key.startswith("credits_"):
        return None
    try:
        return int(key.split("_", 1)[1])
    except (IndexError, ValueError):
        return None
