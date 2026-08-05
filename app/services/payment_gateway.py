"""
Midtrans Payment Gateway Service
Handles payment processing, transaction management, and webhook handling.

Trust model
-----------
Nothing a client sends about money is trusted:

* prices come from `app.services.pricing`, never from the request body;
* a webhook is only acted on if its `signature_key` matches
  SHA512(order_id + status_code + gross_amount + server_key), which only a
  party holding the server key can produce;
* even a correctly signed notification is re-checked against Midtrans'
  `/v2/{order_id}/status` endpoint, so a replayed-but-valid old notification
  cannot resurrect a cancelled order;
* entitlements are granted exactly once per order (`granted_at` on the payment
  record), because Midtrans retries notifications until it gets a 200.
"""

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services import pricing

logger = logging.getLogger(__name__)

# Transaction states that mean "the money is ours".
SETTLED_STATUSES = {"settlement", "capture"}
# States that mean the order will never be paid.
FAILED_STATUSES = {"deny", "cancel", "expire", "failure"}


class MidtransConfig:
    """Midtrans endpoint configuration derived from settings."""

    def __init__(self) -> None:
        self.server_key = settings.midtrans_server_key
        self.client_key = settings.midtrans_client_key
        self.is_production = settings.midtrans_is_production
        self.api_url = (
            "https://api.midtrans.com"
            if self.is_production
            else "https://api.sandbox.midtrans.com"
        )
        self.snap_url = (
            "https://app.midtrans.com"
            if self.is_production
            else "https://app.sandbox.midtrans.com"
        )

    @staticmethod
    def convert_usd_to_idr(usd_amount: float) -> int:
        """Convert USD to IDR at the configured rate."""
        return pricing.usd_to_idr(usd_amount)


midtrans_config = MidtransConfig()


class MidtransPaymentService:
    """
    Midtrans Payment Gateway Service

    Handles transaction creation, status verification, signed webhook
    processing, and refunds.
    """

    def __init__(self) -> None:
        self.server_key = midtrans_config.server_key
        self.client_key = midtrans_config.client_key
        self.is_production = midtrans_config.is_production
        self.api_url = midtrans_config.api_url
        self.snap_url = midtrans_config.snap_url

        # Mock mode is a *development* convenience. It must never engage in
        # production: silently minting fake successful payments there would
        # hand out paid products for free.
        self.mock_mode = not self.server_key and not self.is_production

        if self.is_production and not self.server_key:
            raise RuntimeError(
                "MIDTRANS_SERVER_KEY is required when MIDTRANS_IS_PRODUCTION=true"
            )
        if not self.server_key:
            logger.warning(
                "MIDTRANS_SERVER_KEY not configured - using MOCK mode (development only)"
            )
        if not self.client_key:
            logger.warning("MIDTRANS_CLIENT_KEY not configured - frontend checkout unavailable")

        mode = "MOCK (development)" if self.mock_mode else (
            "PRODUCTION" if self.is_production else "SANDBOX"
        )
        logger.info(
            "Midtrans initialised: %s | api=%s | usd_idr_rate=%s",
            mode, self.api_url, settings.usd_idr_rate,
        )

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _encode_basic_auth(self) -> str:
        """Encode the server key for Basic auth ("ServerKey:" per Midtrans docs)."""
        if not self.server_key:
            return ""
        return base64.b64encode(f"{self.server_key}:".encode()).decode()

    def _get_headers(self) -> Dict[str, str]:
        """Authentication headers for the Midtrans REST API."""
        return {
            "Authorization": f"Basic {self._encode_basic_auth()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_notification_signature(self, payload: Dict[str, Any]) -> bool:
        """
        Verify a Midtrans notification's `signature_key`.

        Midtrans computes SHA512(order_id + status_code + gross_amount + server_key).
        Only a holder of the server key can produce it, which is what makes the
        webhook endpoint safe to expose unauthenticated.
        """
        if not self.server_key:
            # Without a server key there is nothing to verify against; refusing
            # is the only safe answer (mock mode never receives real webhooks).
            logger.error("Cannot verify webhook signature: no server key configured")
            return False

        received = payload.get("signature_key") or ""
        order_id = payload.get("order_id") or ""
        status_code = payload.get("status_code") or ""
        gross_amount = payload.get("gross_amount") or ""

        if not (received and order_id and status_code and gross_amount):
            logger.warning(
                "Webhook rejected: missing signature fields (order_id=%r)", order_id
            )
            return False

        expected = hashlib.sha512(
            f"{order_id}{status_code}{gross_amount}{self.server_key}".encode()
        ).hexdigest()

        # Constant-time compare: a timing oracle here would leak the digest.
        if not hmac_compare(expected, received):
            logger.warning("Webhook rejected: bad signature for order %s", order_id)
            return False
        return True

    # ------------------------------------------------------------------
    # Transaction creation
    # ------------------------------------------------------------------

    async def create_transaction(
        self,
        user_id: str,
        product: str,
        customer_details: Optional[Dict[str, Any]] = None,
        custom_field1: Optional[str] = None,
        custom_field2: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a Snap transaction for `product`.

        The amount is resolved server-side from the product catalogue; there is
        deliberately no `amount` parameter for a caller to influence.
        """
        from app.utils.id_generator import generate_id

        order_id = generate_id(f"payment_{pricing.canonical_product(product)}")
        amount_idr = pricing.resolve_gross_amount_idr(product)
        amount_usd = pricing.resolve_price_usd(product)

        expiry_start = datetime.now(timezone.utc)
        transaction_data: Dict[str, Any] = {
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount_idr,
            },
            "item_details": [
                {
                    "id": pricing.canonical_product(product),
                    "price": amount_idr,
                    "quantity": 1,
                    "name": pricing.product_name(product)[:50],
                }
            ],
            "customer_details": customer_details or {},
            "callbacks": {"finish": settings.payment_finish_redirect_url},
            "expiry": {
                "start_time": expiry_start.strftime("%Y-%m-%d %H:%M:%S +0000"),
                "unit": "hour",
                "duration": settings.payment_expiry_hours,
            },
            "credit_card": {"secure": True},
        }

        if custom_field1:
            transaction_data["custom_field1"] = custom_field1
        if custom_field2:
            transaction_data["custom_field2"] = custom_field2

        if self.mock_mode:
            logger.info("[MOCK] Transaction created: %s (%s IDR)", order_id, amount_idr)
            return {
                "success": True,
                "is_mock": True,
                "order_id": order_id,
                "token": f"mock_token_{order_id}",
                "redirect_url": f"{self.snap_url}/snap/v1/redirection/{order_id}",
                "transaction_id": f"mock_txn_{order_id}",
                "amount_usd": amount_usd,
                "amount_idr": amount_idr,
            }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.snap_url}/snap/v1/transactions",
                    json=transaction_data,
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                result = response.json()

            logger.info("Transaction created: %s (%s IDR)", order_id, amount_idr)
            return {
                "success": True,
                "is_mock": False,
                "order_id": order_id,
                "token": result.get("token"),
                "redirect_url": result.get("redirect_url"),
                "transaction_id": result.get("transaction_id"),
                "amount_usd": amount_usd,
                "amount_idr": amount_idr,
            }

        except httpx.HTTPStatusError as e:
            logger.error(
                "Midtrans API error creating %s: %s - %s",
                order_id, e.response.status_code, e.response.text,
            )
            return {
                "success": False,
                "is_mock": False,
                "error": f"Payment gateway rejected the transaction ({e.response.status_code})",
            }
        except Exception as e:
            logger.error("Error creating transaction %s: %s", order_id, e)
            return {"success": False, "is_mock": False, "error": "Could not reach payment gateway"}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_transaction_status(self, order_id: str) -> Dict[str, Any]:
        """Fetch the authoritative transaction status from Midtrans."""
        if not self.server_key:
            raise ValueError("Midtrans server key not configured")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.api_url}/v2/{order_id}/status",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                result = response.json()

            logger.info(
                "Transaction status: %s - %s", order_id, result.get("transaction_status")
            )
            return {
                "success": True,
                "order_id": order_id,
                "transaction_id": result.get("transaction_id"),
                "transaction_status": result.get("transaction_status"),
                "payment_type": result.get("payment_type"),
                "fraud_status": result.get("fraud_status"),
                "gross_amount": result.get("gross_amount"),
                "status_code": result.get("status_code"),
            }

        except httpx.HTTPStatusError as e:
            logger.error(
                "Error getting status for %s: %s", order_id, e.response.status_code
            )
            return {
                "success": False,
                "error": f"Could not verify transaction ({e.response.status_code})",
            }
        except Exception as e:
            logger.error("Error checking status for %s: %s", order_id, e)
            return {"success": False, "error": "Could not reach payment gateway"}

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    @staticmethod
    def _is_settled(transaction_status: str, fraud_status: str) -> bool:
        """Whether a status pair means the payment is complete and safe to honour."""
        if transaction_status not in SETTLED_STATUSES:
            return False
        # `capture` on a card is only final once fraud screening accepts it;
        # "challenge" means a human still has to review it.
        if fraud_status and fraud_status not in ("accept", ""):
            return False
        return True

    async def settle_order(
        self, order_id: str, expected_user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Verify `order_id` against Midtrans and, if paid, apply its entitlement once.

        The single settlement path shared by the webhook and the browser-driven
        confirm call, so both get identical verification and identical idempotency.

        Ordering is deliberate: the authoritative status is fetched *before* the
        row lock is taken, so a slow Midtrans call never holds a database
        transaction open. The "already granted?" test is then repeated inside the
        lock, which is what makes two concurrent attempts safe rather than merely
        unlikely to collide.
        """
        from app.database import payment_repo

        record = payment_repo.find_by_order_id(order_id)

        if not record:
            # Nothing to apply and retrying will never help, so this is a
            # permanent outcome: the caller should acknowledge, not retry.
            logger.warning("Settlement for unknown order %s", order_id)
            return {
                "success": False, "permanent": True,
                "error": "Unknown order", "order_id": order_id,
            }

        if expected_user_id and record.get("user_id") != expected_user_id:
            logger.warning(
                "Settlement for %s rejected: caller %s does not own it",
                order_id, expected_user_id,
            )
            return {
                "success": False, "permanent": True,
                "error": "Order does not belong to caller",
            }

        # Already honoured — Midtrans retries, so a duplicate notification is the
        # normal path here, not an error.
        if record.get("granted_at"):
            logger.info("Order %s already granted at %s", order_id, record["granted_at"])
            return {
                "success": True, "order_id": order_id,
                "status": record.get("status"), "already_granted": True,
            }

        is_mock = bool(record.get("is_mock"))
        if is_mock and self.is_production:
            logger.error("Refusing to settle mock order %s in production", order_id)
            return {
                "success": False, "permanent": True,
                "error": "Mock payments cannot settle in production",
            }

        # --- Establish the authoritative status, outside any lock -------------
        if is_mock:
            status = {
                "success": True,
                "transaction_status": "settlement",
                "fraud_status": "accept",
                "gross_amount": str(record.get("amount_idr") or 0),
                "payment_type": "mock",
                "transaction_id": record.get("transaction_id"),
            }
        else:
            status = await self.get_transaction_status(order_id)
            if not status.get("success"):
                # Transient: let the caller retry (Midtrans will re-notify).
                return {"success": False, "error": status.get("error", "Verification failed")}

        transaction_status = status.get("transaction_status") or ""
        fraud_status = status.get("fraud_status") or ""
        common = {
            "transaction_status": transaction_status,
            "fraud_status": fraud_status,
            "payment_type": status.get("payment_type"),
            "gateway_response": status,
        }

        # --- Decide and apply, under the row lock ----------------------------
        with payment_repo.SettlementLock(order_id) as lock:
            locked = lock.record
            if not locked:
                return {
                    "success": False, "permanent": True,
                    "error": "Unknown order", "order_id": order_id,
                }

            # Re-check under the lock: a concurrent attempt may have granted
            # between our read above and acquiring the lock.
            if locked.get("granted_at"):
                logger.info("Order %s granted concurrently; nothing to do", order_id)
                return {
                    "success": True, "order_id": order_id,
                    "status": locked.get("status"), "already_granted": True,
                }

            if not self._is_settled(transaction_status, fraud_status):
                new_status = "failed" if transaction_status in FAILED_STATUSES else "pending"
                lock.update(status=new_status, **common)
                logger.info(
                    "Order %s not settled (status=%s fraud=%s)",
                    order_id, transaction_status, fraud_status,
                )
                return {
                    "success": True, "order_id": order_id, "status": new_status,
                    "transaction_status": transaction_status, "granted": False,
                }

            # The sum Midtrans actually collected must match what we priced.
            expected_idr = locked.get("amount_idr")
            if expected_idr and not is_mock:
                try:
                    paid_idr = int(float(status.get("gross_amount") or 0))
                except (TypeError, ValueError):
                    paid_idr = 0
                if paid_idr != int(expected_idr):
                    logger.error(
                        "Order %s amount mismatch: expected %s IDR, Midtrans reports %s IDR",
                        order_id, expected_idr, paid_idr,
                    )
                    lock.update(status="amount_mismatch", **common)
                    # Retrying cannot fix a wrong amount — flag for a human.
                    return {
                        "success": False, "permanent": True,
                        "error": "Payment amount mismatch", "order_id": order_id,
                    }

            # Apply the entitlement, then mark granted. Marking last means a
            # crash mid-grant leaves the order retryable rather than silently
            # consumed; the grant itself is idempotent on order_id in Postgres,
            # so a retry cannot double-apply.
            from app.services.entitlements import apply_entitlement

            granted, detail = await apply_entitlement(
                user_id=locked.get("user_id"),
                product=locked.get("product"),
                amount_usd=float(locked.get("amount") or 0),
                order_id=order_id,
            )

            if not granted:
                lock.update(status="paid_grant_failed", grant_error=detail, **common)
                logger.error("Order %s paid but entitlement failed: %s", order_id, detail)
                return {
                    "success": False,
                    "error": "Payment recorded but entitlement failed",
                    "order_id": order_id,
                }

            lock.update(
                status="paid",
                granted_at=datetime.now(timezone.utc),
                grant_detail={"detail": detail},
                **common,
            )
            # Only a settled order gets an invoice number.
            payment_repo.assign_invoice_number(lock)
            settled_record = dict(lock.record or locked)

        # Notify outside the lock: a slow SMTP call must not hold a row lock, and
        # a failed notification must never undo a successful payment.
        await self._notify_settled(settled_record, detail)

        logger.info("Order %s settled and entitlement applied", order_id)
        return {
            "success": True, "order_id": order_id, "status": "paid",
            "transaction_status": transaction_status, "granted": True,
            "detail": detail,
        }

    async def _notify_settled(self, record, detail: str) -> None:
        """Fire purchase notifications, swallowing any failure."""
        try:
            from app.services import notifications

            await notifications.payment_settled(record, detail)
        except Exception as e:
            logger.error(
                "Notification failed for order %s (payment stands): %s",
                record.get("order_id"), e,
            )

    async def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a Midtrans HTTP notification.

        The caller must have already verified the signature; this only decides
        what to do with a trusted notification.
        """
        order_id = payload.get("order_id", "")
        logger.info(
            "Webhook accepted: %s - %s - %s",
            order_id,
            payload.get("transaction_status"),
            payload.get("fraud_status"),
        )
        return await self.settle_order(order_id)

    # ------------------------------------------------------------------
    # Refunds
    # ------------------------------------------------------------------

    async def refund_payment(self, order_id: str, amount: Optional[int] = None) -> Dict[str, Any]:
        """Refund a settled payment (admin-only at the route layer)."""
        if not self.server_key:
            raise ValueError("Midtrans server key not configured")

        try:
            refund_data: Dict[str, Any] = {}
            if amount:
                refund_data["amount"] = amount

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.api_url}/v2/{order_id}/refund",
                    json=refund_data,
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                result = response.json()

            logger.info("Refund processed: %s", order_id)
            return {
                "success": True,
                "order_id": order_id,
                "refund_id": result.get("refund_id"),
                "refund_amount": result.get("refund_amount"),
            }

        except httpx.HTTPStatusError as e:
            logger.error("Refund API error for %s: %s", order_id, e.response.status_code)
            return {"success": False, "error": f"Refund rejected ({e.response.status_code})"}
        except Exception as e:
            logger.error("Error refunding %s: %s", order_id, e)
            return {"success": False, "error": "Could not reach payment gateway"}

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def get_client_key(self) -> Optional[str]:
        """The Snap client key, safe to expose to the browser."""
        return self.client_key

    def is_configured(self) -> bool:
        """Whether both Midtrans keys are present."""
        return bool(self.server_key and self.client_key)

    @property
    def snap_js_url(self) -> str:
        """The Snap.js bundle matching the active environment."""
        return f"{self.snap_url}/snap/snap.js"


def hmac_compare(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    import hmac

    return hmac.compare_digest(a.lower(), b.lower())


# Global service instance
midtrans_service = MidtransPaymentService()
