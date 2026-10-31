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
#
# Subscription ids follow the 2026 tier rebrand and are the SAME strings the
# pricing page publishes and the identity service stores as a user's tier:
# `operational` ($20) and `business` ($99). The pre-rebrand ids
# (foundation / pro / acceleration / intelligence) survive only as read
# aliases below, so historical orders stay resolvable.
#
# `enterprise` is deliberately absent: the pricing page sells it as
# "Custom / Talk to Sales" and never publishes a figure, so a self-serve
# checkout for it would charge an amount no customer was ever shown. Naming it
# here again would silently reinstate that.
FIXED_PRICES_USD: Dict[str, int] = {
    "ai_snapshot": 79,
    "ai_blueprint": 249,
    # Snapshot + Blueprint + Roadmap together as the Complete Transformation
    # Package. The dashboard and the settings modal have advertised this
    # bundle since launch; it was not sellable, so every click on it 400'd.
    "ai_fullstack": 299,
    "operational": 20,
    "business": 99,
}

# Annual billing for the Operating Plan: twelve months paid up front, at the
# monthly price times twelve (no annual discount). Derived rather than typed so
# the two can never drift apart; avry-backend grants these 365 days of the same
# tier (`ANNUAL_PRODUCTS` in routes/entitlements.py).
ANNUAL_BILLING_MONTHS = 12
ANNUAL_PRODUCTS: Dict[str, str] = {
    "operational_annual": "operational",
    "business_annual": "business",
}
for _annual, _monthly in ANNUAL_PRODUCTS.items():
    FIXED_PRICES_USD[_annual] = FIXED_PRICES_USD[_monthly] * ANNUAL_BILLING_MONTHS

# Credit top-up packs, in USD, keyed by the credits granted.
#
# A credit is $0.004365 of LLM spend (CREDIT_COST_USD in avry-backend's
# app/services/tiers.py; the bridge charges each agent turn by what OpenRouter
# billed). Every pack keeps at least a 65% margin over that at peak DeepSeek
# rates, after the ~3% gateway fee; the smallest pack sits at 70% and the
# discount deepens with size. Bought credits land in the account's extra
# balance and never expire.
#
# Keep this table, `creditPackages` in the dashboard's SettingsModal.tsx and
# CreditPurchaseTab.tsx, and CREDIT_PACK_DEFINITIONS in the marketing site's
# src/lib/pricing.ts in step: the server prices every order from here (no
# amount is accepted from the browser), so a divergence is charged silently
# rather than rejected.
CREDIT_PACKS_USD: Dict[int, int] = {
    1000: 15,     # $0.0150/credit, 70.0% margin
    1700: 25,     # $0.0147/credit, 69.4%
    3500: 50,     # $0.0143/credit, 68.5%
    7200: 100,    # $0.0139/credit, 67.6%
    11000: 150,   # $0.0136/credit, 67.0%
    18500: 250,   # $0.0135/credit, 66.7%
    37500: 500,   # $0.0133/credit, 66.2%
    76000: 1000,  # $0.0132/credit, 65.8%
}

# Legacy/alternate ids the frontend has used for the same products.
#
# The subscription entries are the pre-2026-rebrand names. They are read-only
# aliases: nothing new should be authored with them, but orders placed before
# the rebrand carry them and must keep resolving to the right plan and price.
ALIASES: Dict[str, str] = {
    "foundation": "operational",
    "pro": "business",
    "acceleration": "business",
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
    "ai_snapshot": "Business Operations Assessment",
    "ai_blueprint": "Transformation Blueprint",
    "ai_fullstack": "Complete Transformation Package",
    "operational": "Operational Plan",
    "business": "Business Plan",
    "operational_annual": "Operational Plan (Annual)",
    "business_annual": "Business Plan (Annual)",
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


def resolve_charge(
    product: str, discount_usd: float = 0.0, allow_free: bool = False
) -> "tuple[float, int]":
    """
    The (USD, IDR) actually charged for `product` after a server-derived discount.

    `discount_usd` must come from `promo.quote()`, never from a request body.
    Raises UnknownProduct if the discounted total falls under Midtrans' floor.
    A total of zero (a 100% promo) is (0.0, 0) when `allow_free`; such an
    order never reaches Midtrans, so the gateway paths leave it off.
    """
    list_usd = resolve_price_usd(product)
    net_usd = round(list_usd - max(discount_usd, 0.0), 2)
    if net_usd <= 0:
        if allow_free:
            return 0.0, 0
        raise UnknownProduct(f"{product} would be charged at or below zero")
    idr = usd_to_idr(net_usd)
    if idr < MIN_GROSS_AMOUNT_IDR:
        raise UnknownProduct(
            f"{product} converts to Rp {idr}, below Midtrans' Rp {MIN_GROSS_AMOUNT_IDR} minimum"
        )
    return net_usd, idr


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
