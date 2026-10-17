"""
Promo code rules.

Pure functions plus one orchestrating `quote()`. The route layer never computes a
discount itself: like prices (see `pricing`), a discount is only ever derived on
the server from a stored code, so a client cannot name its own figure.

Scope decisions:

* Wallet top-ups are never discountable. The charge *is* the credit, so a
  discount there would mint wallet balance out of nothing.
* A discount applies to the single charge it is used on. It does not make a
  subscription renewal cheaper later.
* The entitlement is still granted at list value; only the charge is reduced.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database import promo_repo
from app.services import pricing

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")

# No 0/O/1/I/L: generated codes get read aloud and retyped from screenshots.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

MAX_PERCENT = 99  # 100% would be a free order, which Midtrans cannot charge
MAX_BULK = 500


class PromoError(Exception):
    """A code that cannot be used. `reason` is a stable machine-readable key."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def normalize_code(raw: Optional[str]) -> str:
    code = (raw or "").strip().upper().replace(" ", "")
    if not CODE_RE.match(code):
        raise PromoError("invalid_format", "That promo code isn't valid.")
    return code


def compute_discount_usd(list_usd: float, discount_type: str, value: float) -> float:
    """The discount for one order, never more than the price itself."""
    if discount_type == "percent":
        raw = list_usd * (value / 100.0)
    elif discount_type == "fixed_usd":
        raw = value
    else:  # pragma: no cover - the table CHECK prevents this
        raise PromoError("invalid", "That promo code isn't valid.")
    return round(min(max(raw, 0.0), list_usd), 2)


def _product_allowed(allowed: Optional[List[str]], product: str) -> bool:
    if not allowed:
        return True
    return any(product == p or (p.endswith("_") and product.startswith(p)) for p in allowed)


def evaluate(
    promo: Dict[str, Any],
    product: str,
    list_usd: float,
    usage: Dict[str, int],
    now: Optional[datetime] = None,
) -> float:
    """Return the discount in USD, or raise PromoError saying why not."""
    now = now or datetime.now(timezone.utc)

    if product.startswith("wallet_topup_"):
        raise PromoError("not_applicable", "Promo codes can't be used on wallet top-ups.")
    if not promo.get("active"):
        raise PromoError("inactive", "That promo code isn't active.")

    valid_from = promo.get("valid_from")
    if valid_from and datetime.fromisoformat(valid_from) > now:
        raise PromoError("not_started", "That promo code isn't valid yet.")
    expires_at = promo.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) <= now:
        raise PromoError("expired", "That promo code has expired.")

    if not _product_allowed(promo.get("products"), product):
        raise PromoError("not_applicable", "That promo code doesn't apply to this product.")

    if usage["used_by_user"] >= promo.get("per_user_limit", 1):
        raise PromoError("already_used", "You've already used this promo code.")
    cap = promo.get("max_redemptions")
    if cap is not None and usage["total_used"] >= cap:
        raise PromoError("exhausted", "That promo code has been fully redeemed.")

    discount = compute_discount_usd(list_usd, promo["discount_type"], promo["discount_value"])
    if discount <= 0:
        raise PromoError("not_applicable", "That promo code doesn't change the price.")
    return discount


@dataclass
class Quote:
    code: str
    product: str
    list_usd: float
    discount_usd: float
    final_usd: float
    final_idr: int
    list_idr: int

    def public(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "product": self.product,
            "list_usd": self.list_usd,
            "discount_usd": self.discount_usd,
            "final_usd": self.final_usd,
            "list_idr": self.list_idr,
            "final_idr": self.final_idr,
        }


def quote(raw_code: str, product: str, user_id: str) -> Quote:
    """
    Validate `raw_code` for `product` and price the order with it applied.

    Raises PromoError for an unusable code, or pricing.UnknownProduct for an
    unsellable product / a discounted total below the gateway's minimum.
    """
    code = normalize_code(raw_code)
    canonical = pricing.canonical_product(product)
    list_usd = pricing.resolve_price_usd(product)

    promo = promo_repo.get(code)
    # Same message as an inactive code: don't confirm which codes exist.
    if not promo:
        raise PromoError("not_found", "That promo code isn't valid.")

    discount = evaluate(promo, canonical, list_usd, promo_repo.usage(code, user_id))
    final_usd, final_idr = pricing.resolve_charge(product, discount)
    return Quote(
        code=code,
        product=canonical,
        list_usd=list_usd,
        discount_usd=discount,
        final_usd=final_usd,
        final_idr=final_idr,
        list_idr=pricing.usd_to_idr(list_usd),
    )


def generate_code(prefix: str = "", length: int = 8) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}" if prefix else body


# Legal-entity words that say nothing about who the affiliate is.
_ENTITY_WORDS = {
    "PT", "CV", "TBK", "UD", "PD", "FIRMA", "KOPERASI", "YAYASAN",
    "LTD", "LLC", "INC", "CORP", "CO", "COMPANY", "GROUP", "THE",
}
AFFILIATE_PREFIX_MAX = 12


def affiliate_prefix(name: Optional[str], company: Optional[str]) -> str:
    """
    A readable code prefix that identifies the affiliate, e.g.
    "PT Distributor Nusantara" -> "DISTRIBUTORNU", "Budi Santoso" -> "BUDISANTOSO".

    The company wins over the person, because a code is usually tracked per
    company. Combined with the random suffix from `generate_code`, every code
    is unique while still showing at a glance who it belongs to. Returns ""
    when neither yields any usable letters.
    """
    for source in (company, name):
        words = re.findall(r"[A-Z0-9]+", (source or "").upper())
        words = [w for w in words if w not in _ENTITY_WORDS] or words
        joined = "".join(words)[:AFFILIATE_PREFIX_MAX]
        if len(joined) >= 2:
            return joined
    return ""
