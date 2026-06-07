"""
Purchase Service - Handles product purchases and user updates
Processes purchases for subscriptions, features, and credits.
"""

import logging
from typing import Tuple, Dict, Any
from datetime import datetime

from app.database.db_service import db
from app.services.wallet_service import wallet_service
from app.models.wallet import TransactionType

logger = logging.getLogger(__name__)


class PurchaseService:
    """Service for handling purchases and user updates"""

    @staticmethod
    def process_purchase(
        user_id: str,
        product: str,
        amount: float,
        order_id: str,
        is_mock: bool = False,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Process a purchase and update user accordingly.
        
        Args:
            user_id: User identifier
            product: Product being purchased
            amount: Amount paid (in USD)
            order_id: Payment order ID
            is_mock: Whether this is a mock payment (for development)
            
        Returns:
            Tuple of (success, data, error_message)
        """
        try:
            # Load user data
            users = db.load_all_json("users")
            user = next((u for u in users if u.get("user_id") == user_id), None)
            
            if not user:
                return False, {}, f"User {user_id} not found"
            
            user_updated = False
            
            # Handle one-time feature purchases
            if product == "ai_diagnostic":
                user["has_diagnostic"] = True
                user_updated = True
                logger.info(f"User {user_id} unlocked: AI Diagnostic (Mock: {is_mock})")
            
            elif product == "ai_blueprint":
                user["has_blueprint"] = True
                user_updated = True
                logger.info(f"User {user_id} unlocked: AI Blueprint (Mock: {is_mock})")
            
            elif product == "ai_fullstack":
                user["has_diagnostic"] = True
                user["has_blueprint"] = True
                user_updated = True
                logger.info(f"User {user_id} unlocked: Full Stack (Mock: {is_mock})")
            
            # Handle subscription changes
            elif product == "foundation":
                user["tier"] = "foundation"
                user["is_subscribed"] = True
                user["subscription_start"] = datetime.utcnow().isoformat()
                user["credits_max"] = 50  # 50 IC/month
                user["credits"] = 50
                user_updated = True
                logger.info(f"User {user_id} subscribed to: Foundation tier (Mock: {is_mock})")
            
            elif product == "acceleration":
                user["tier"] = "acceleration"
                user["is_subscribed"] = True
                user["subscription_start"] = datetime.utcnow().isoformat()
                user["credits_max"] = 300  # 300 IC/month
                user["credits"] = 300
                user_updated = True
                logger.info(f"User {user_id} subscribed to: Acceleration (Pro) tier (Mock: {is_mock})")
            
            elif product == "intelligence":
                user["tier"] = "intelligence"
                user["is_subscribed"] = True
                user["subscription_start"] = datetime.utcnow().isoformat()
                user["credits_max"] = 2000  # 2000 IC/month
                user["credits"] = 2000
                user_updated = True
                logger.info(f"User {user_id} subscribed to: Intelligence (Enterprise) tier (Mock: {is_mock})")
            
            # Handle credit purchases
            elif product.startswith("credits_"):
                # Extract credit amount
                credit_amount = int(product.split("_")[1])
                if "credits" not in user:
                    user["credits"] = 0
                user["credits"] += credit_amount
                user_updated = True
                logger.info(f"User {user_id} purchased: {credit_amount} credits (Mock: {is_mock})")
            
            # Handle wallet topups
            elif product.startswith("wallet_topup_"):
                # Topup amount is embedded in product name
                topup_amount = float(product.split("_")[2])
                if "wallet_balance" not in user:
                    user["wallet_balance"] = 0.0
                user["wallet_balance"] += topup_amount
                user_updated = True
                logger.info(f"User {user_id} topped up wallet: ${topup_amount} (Mock: {is_mock})")
            
            # Update user record if changes were made
            if user_updated:
                user["updated_at"] = datetime.utcnow().isoformat()
                db.save_json("users", user_id, user)
            
            # Record transaction in wallet (for audit trail)
            wallet_service.create_transaction(
                user_id=user_id,
                transaction_type=TransactionType.TOPUP,
                amount=amount,
                description=f"Purchase: {product} (Mock: {is_mock})",
                reference_id=order_id,
                metadata={
                    "product": product,
                    "order_id": order_id,
                    "payment_method": "payment_gateway",
                    "is_mock": is_mock,
                }
            )
            
            return True, {
                "user_id": user_id,
                "product": product,
                "tier": user.get("tier"),
                "is_subscribed": user.get("is_subscribed"),
                "has_diagnostic": user.get("has_diagnostic"),
                "has_blueprint": user.get("has_blueprint"),
                "credits": user.get("credits"),
                "is_mock": is_mock,
                "updated_at": user.get("updated_at"),
            }, ""
        
        except Exception as e:
            error_msg = f"Error processing purchase: {str(e)}"
            logger.error(error_msg)
            return False, {}, error_msg

    @staticmethod
    def refund_purchase(
        user_id: str,
        product: str,
        amount: float,
        order_id: str,
    ) -> Tuple[bool, str]:
        """
        Refund a purchase and revert user changes.
        
        Args:
            user_id: User identifier
            product: Product being refunded
            amount: Refund amount (in USD)
            order_id: Original payment order ID
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Load user data
            users = db.load_all_json("users")
            user = next((u for u in users if u.get("user_id") == user_id), None)
            
            if not user:
                return False, f"User {user_id} not found"
            
            # Revert changes based on product
            if product == "ai_diagnostic":
                user["has_diagnostic"] = False
            elif product == "ai_blueprint":
                user["has_blueprint"] = False
            elif product == "ai_fullstack":
                user["has_diagnostic"] = False
                user["has_blueprint"] = False
            elif product.startswith("credits_"):
                credit_amount = int(product.split("_")[1])
                if "credits" not in user:
                    user["credits"] = 0
                user["credits"] = max(0, user["credits"] - credit_amount)
            
            # Update user record
            user["updated_at"] = datetime.utcnow().isoformat()
            db.save_json("users", user_id, user)
            
            # Record refund in wallet
            wallet_service.refund_purchase(
                user_id=user_id,
                amount=amount,
                order_id=order_id,
            )
            
            logger.info(f"Refunded purchase for user {user_id}: {product}")
            return True, ""
        
        except Exception as e:
            error_msg = f"Error processing refund: {str(e)}"
            logger.error(error_msg)
            return False, error_msg
