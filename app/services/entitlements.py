"""
Applying entitlements once a payment is verified.

Identity lives in avry-backend (Postgres `identity.users` / `identity.user_tiers`
/ `billing.user_credits`), so a paid tier or credit pack has to be applied
*there* to have any effect. This module is the bridge: it calls avry-backend's
internal entitlement API with the shared service token.

Wallet top-ups are the exception — the wallet is this service's own concern, so
they are applied locally.

`apply_entitlement` returns (ok, detail). A False result means the payment must
NOT be marked as granted, so it can be retried (Midtrans will keep re-notifying,
and an admin can replay a confirm).
"""

import logging
from typing import Tuple

import httpx

from app.config import settings
from app.services import pricing

logger = logging.getLogger(__name__)

GRANT_TIMEOUT_SECONDS = 20.0


async def apply_entitlement(
    user_id: str,
    product: str,
    amount_usd: float,
    order_id: str,
) -> Tuple[bool, str]:
    """
    Give `user_id` what they paid for. Safe to call more than once per order.

    Returns (success, human-readable detail).
    """
    if not user_id or not product:
        return False, "missing user_id or product"

    canonical = pricing.canonical_product(product)

    # Wallet top-ups only touch this service's wallet store.
    if canonical.startswith("wallet_topup_"):
        return _apply_wallet_topup(user_id, canonical, amount_usd, order_id)

    if not settings.internal_service_token:
        logger.error(
            "INTERNAL_SERVICE_TOKEN not configured — cannot apply entitlement for %s",
            order_id,
        )
        return False, "internal service token not configured"

    url = f"{settings.avry_backend_url.rstrip('/')}/api/v1/entitlements/internal/grant"
    payload = {
        "user_id": user_id,
        "product": canonical,
        "order_id": order_id,
        "amount_usd": amount_usd,
    }

    try:
        async with httpx.AsyncClient(timeout=GRANT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"X-Internal-Token": settings.internal_service_token},
            )
    except Exception as e:
        logger.error("Entitlement call failed for order %s: %s", order_id, e)
        return False, f"could not reach identity service: {e}"

    if response.status_code == 200:
        body = response.json()
        if body.get("already_granted"):
            return True, f"already granted ({canonical})"
        return True, f"granted {canonical}: {body.get('detail')}"

    # 400 = no mapping for this product; that is a configuration bug, not a
    # transient failure, so surface it loudly but still refuse to mark granted.
    logger.error(
        "Entitlement rejected for order %s: %s %s",
        order_id, response.status_code, response.text[:300],
    )
    return False, f"identity service returned {response.status_code}"


def _apply_wallet_topup(
    user_id: str, product: str, amount_usd: float, order_id: str
) -> Tuple[bool, str]:
    """Credit a verified top-up to the user's wallet in this service."""
    try:
        topup_usd = float(product.split("_", 2)[2])
    except (IndexError, ValueError):
        return False, f"malformed wallet top-up product: {product}"

    # The charge is derived from the product id, so these should never diverge;
    # if they do, something upstream rewrote the amount.
    if abs(topup_usd - amount_usd) > 0.01:
        logger.error(
            "Wallet top-up mismatch on order %s: product says $%s, record says $%s",
            order_id, topup_usd, amount_usd,
        )
        return False, "wallet top-up amount mismatch"

    try:
        from app.models.wallet import TransactionType
        from app.services.wallet_service import wallet_service

        wallet_service.create_transaction(
            user_id=user_id,
            transaction_type=TransactionType.TOPUP,
            amount=topup_usd,
            description=f"Wallet top-up ${topup_usd}",
            reference_id=order_id,
            metadata={"product": product, "order_id": order_id, "payment_method": "midtrans"},
        )
    except Exception as e:
        logger.error("Wallet top-up failed for order %s: %s", order_id, e)
        return False, f"wallet top-up failed: {e}"

    return True, f"wallet credited ${topup_usd}"
