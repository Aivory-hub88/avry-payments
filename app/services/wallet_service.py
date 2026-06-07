"""
Wallet Service - Manages user wallet and transaction operations
Implements secure transaction handling and audit trail.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from decimal import Decimal

from app.database.db_service import DatabaseService
from app.utils.id_generator import generate_id

# Initialize database service
db_service = DatabaseService()
from app.models.wallet import (
    Wallet, WalletTransaction, TransactionType, TransactionStatus,
    PaymentCard
)

logger = logging.getLogger(__name__)


class WalletService:
    """Service for managing user wallets and transactions"""

    # ======================================================================
    # Wallet Management
    # ======================================================================

    @staticmethod
    def get_or_create_wallet(user_id: str) -> Wallet:
        """
        Get existing wallet or create new one.
        
        Args:
            user_id: User identifier
            
        Returns:
            Wallet object
        """
        try:
            # Try to load existing wallet
            wallets = db_service.load_all_json("wallets")
            wallet_data = next((w for w in wallets if w.get("user_id") == user_id), None)
            
            if wallet_data:
                return Wallet(**wallet_data)
            
            # Create new wallet
            wallet = Wallet(
                wallet_id=generate_id("wallet"),
                user_id=user_id,
                balance=0.0,
                total_topup=0.0,
                total_spent=0.0,
                total_refunded=0.0,
                currency="USD",
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            
            # Save wallet
            wallet_dict = wallet.model_dump(mode='json')
            db_service.save_json("wallets", wallet.wallet_id, wallet_dict)
            logger.info(f"Created new wallet for user {user_id}: {wallet.wallet_id}")
            
            return wallet
            
        except Exception as e:
            logger.error(f"Error getting/creating wallet for user {user_id}: {str(e)}")
            raise

    @staticmethod
    def get_wallet(user_id: str) -> Optional[Wallet]:
        """Get wallet for user"""
        try:
            wallets = db_service.load_all_json("wallets")
            wallet_data = next((w for w in wallets if w.get("user_id") == user_id), None)
            return Wallet(**wallet_data) if wallet_data else None
        except Exception as e:
            logger.error(f"Error loading wallet for user {user_id}: {str(e)}")
            return None

    @staticmethod
    def get_wallet_balance(user_id: str) -> float:
        """Get current wallet balance"""
        wallet = WalletService.get_wallet(user_id)
        return wallet.balance if wallet else 0.0

    # ======================================================================
    # Transaction Management
    # ======================================================================

    @staticmethod
    def create_transaction(
        user_id: str,
        transaction_type: TransactionType,
        amount: float,
        description: str,
        reference_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Tuple[bool, Optional[WalletTransaction], str]:
        """
        Create a new transaction (internal use).
        
        Args:
            user_id: User identifier
            transaction_type: Type of transaction
            amount: Amount in USD
            description: Transaction description
            reference_id: Order/Purchase ID reference
            metadata: Additional metadata
            
        Returns:
            Tuple of (success, transaction, error_message)
        """
        try:
            wallet = WalletService.get_or_create_wallet(user_id)
            
            # Calculate new balance
            if transaction_type == TransactionType.TOPUP:
                new_balance = wallet.balance + amount
            elif transaction_type == TransactionType.PURCHASE:
                if wallet.balance < amount:
                    return False, None, "Insufficient wallet balance"
                new_balance = wallet.balance - amount
            elif transaction_type == TransactionType.REFUND:
                new_balance = wallet.balance + amount
            elif transaction_type == TransactionType.ADJUSTMENT:
                new_balance = wallet.balance + amount  # Can be positive or negative
            else:
                return False, None, f"Unknown transaction type: {transaction_type}"

            # Ensure non-negative balance for purchases
            if new_balance < 0 and transaction_type == TransactionType.PURCHASE:
                return False, None, "Transaction would result in negative balance"

            # Create transaction record
            transaction = WalletTransaction(
                transaction_id=generate_id("txn"),
                user_id=user_id,
                type=transaction_type,
                amount=amount,
                balance_before=wallet.balance,
                balance_after=new_balance,
                description=description,
                reference_id=reference_id,
                status=TransactionStatus.COMPLETED,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                metadata=metadata or {},
            )

            # Update wallet
            wallet.balance = new_balance
            wallet.last_transaction_at = datetime.utcnow()
            wallet.updated_at = datetime.utcnow()

            # Update totals based on transaction type
            if transaction_type == TransactionType.TOPUP:
                wallet.total_topup += amount
            elif transaction_type == TransactionType.PURCHASE:
                wallet.total_spent += amount
            elif transaction_type == TransactionType.REFUND:
                wallet.total_refunded += amount

            # Save both transaction and wallet
            wallet_dict = wallet.model_dump(mode='json')
            db_service.save_json("wallets", wallet.wallet_id, wallet_dict)
            transaction_dict = transaction.model_dump(mode='json')
            db_service.save_json("wallet_transactions", transaction.transaction_id, transaction_dict)

            logger.info(
                f"Transaction created - User: {user_id}, Type: {transaction_type}, "
                f"Amount: {amount}, New Balance: {new_balance}"
            )

            return True, transaction, ""

        except Exception as e:
            error_msg = f"Error creating transaction: {str(e)}"
            logger.error(error_msg)
            return False, None, error_msg

    @staticmethod
    def topup_wallet(user_id: str, amount: float, order_id: str) -> Tuple[bool, Dict, str]:
        """
        Add funds to wallet (called after successful Midtrans payment).
        
        Args:
            user_id: User identifier
            amount: Amount to add
            order_id: Midtrans order ID
            
        Returns:
            Tuple of (success, wallet_info, error_message)
        """
        success, transaction, error = WalletService.create_transaction(
            user_id=user_id,
            transaction_type=TransactionType.TOPUP,
            amount=amount,
            description=f"Wallet topup via Midtrans",
            reference_id=order_id,
            metadata={"order_id": order_id, "payment_method": "midtrans"},
        )

        if success:
            wallet = WalletService.get_wallet(user_id)
            return True, {
                "wallet_id": wallet.wallet_id,
                "balance": wallet.balance,
                "total_topup": wallet.total_topup,
                "transaction_id": transaction.transaction_id,
            }, ""
        else:
            return False, {}, error

    @staticmethod
    def deduct_for_purchase(
        user_id: str,
        amount: float,
        product: str,
        order_id: str,
    ) -> Tuple[bool, Dict, str]:
        """
        Deduct amount from wallet for purchase.
        
        Args:
            user_id: User identifier
            amount: Amount to deduct
            product: Product being purchased
            order_id: Purchase order ID
            
        Returns:
            Tuple of (success, transaction_info, error_message)
        """
        success, transaction, error = WalletService.create_transaction(
            user_id=user_id,
            transaction_type=TransactionType.PURCHASE,
            amount=amount,
            description=f"Purchase: {product}",
            reference_id=order_id,
            metadata={"product": product, "order_id": order_id},
        )

        if success:
            wallet = WalletService.get_wallet(user_id)
            return True, {
                "transaction_id": transaction.transaction_id,
                "balance_before": transaction.balance_before,
                "balance_after": transaction.balance_after,
                "wallet_balance": wallet.balance,
            }, ""
        else:
            return False, {}, error

    @staticmethod
    def refund_purchase(
        user_id: str,
        amount: float,
        order_id: str,
    ) -> Tuple[bool, Dict, str]:
        """
        Refund purchase amount back to wallet.
        
        Args:
            user_id: User identifier
            amount: Amount to refund
            order_id: Original purchase order ID
            
        Returns:
            Tuple of (success, transaction_info, error_message)
        """
        success, transaction, error = WalletService.create_transaction(
            user_id=user_id,
            transaction_type=TransactionType.REFUND,
            amount=amount,
            description=f"Refund for order: {order_id}",
            reference_id=order_id,
            metadata={"original_order_id": order_id},
        )

        if success:
            wallet = WalletService.get_wallet(user_id)
            return True, {
                "transaction_id": transaction.transaction_id,
                "balance_after": transaction.balance_after,
                "wallet_balance": wallet.balance,
            }, ""
        else:
            return False, {}, error

    @staticmethod
    def get_transaction_history(user_id: str, limit: int = 50) -> List[Dict]:
        """Get transaction history for user"""
        try:
            transactions = db_service.load_all_json("wallet_transactions")
            user_txns = [t for t in transactions if t.get("user_id") == user_id]
            
            # Sort by created_at descending
            user_txns.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            
            return user_txns[:limit]
        except Exception as e:
            logger.error(f"Error loading transaction history for user {user_id}: {str(e)}")
            return []

    # ======================================================================
    # Payment Card Management
    # ======================================================================

    @staticmethod
    def save_payment_card(
        user_id: str,
        token: str,
        brand: str,
        last_four: str,
        holder_name: str,
        expiry_month: int,
        expiry_year: int,
        is_default: bool = False,
    ) -> Tuple[bool, Optional[PaymentCard], str]:
        """
        Save a new payment card for user.
        
        Args:
            user_id: User identifier
            token: Midtrans card token
            brand: Card brand (visa, mastercard, etc)
            last_four: Last 4 digits of card
            holder_name: Cardholder name
            expiry_month: Expiry month
            expiry_year: Expiry year
            is_default: Whether to set as default
            
        Returns:
            Tuple of (success, card, error_message)
        """
        try:
            card = PaymentCard(
                card_id=generate_id("card"),
                user_id=user_id,
                token=token,
                brand=brand.lower(),
                last_four=last_four,
                holder_name=holder_name,
                expiry_month=expiry_month,
                expiry_year=expiry_year,
                is_default=is_default,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )

            # If setting as default, unset others
            if is_default:
                cards = db_service.load_all_json("payment_cards")
                for c in cards:
                    if c.get("user_id") == user_id and c.get("is_default"):
                        c["is_default"] = False
                        db_service.save_json("payment_cards", c.get("card_id"), c)

            card_dict = card.model_dump(mode='json')
            db_service.save_json("payment_cards", card.card_id, card_dict)
            logger.info(f"Saved payment card for user {user_id}: {card.card_id}")
            
            return True, card, ""
        except Exception as e:
            error_msg = f"Error saving payment card: {str(e)}"
            logger.error(error_msg)
            return False, None, error_msg

    @staticmethod
    def get_payment_cards(user_id: str) -> List[PaymentCard]:
        """Get all payment cards for user"""
        try:
            cards = db_service.load_all_json("payment_cards")
            user_cards = [PaymentCard(**c) for c in cards if c.get("user_id") == user_id and c.get("is_active")]
            return user_cards
        except Exception as e:
            logger.error(f"Error loading payment cards for user {user_id}: {str(e)}")
            return []

    @staticmethod
    def get_default_card(user_id: str) -> Optional[PaymentCard]:
        """Get default payment card for user"""
        try:
            cards = db_service.load_all_json("payment_cards")
            card_data = next(
                (c for c in cards if c.get("user_id") == user_id and c.get("is_default") and c.get("is_active")),
                None
            )
            return PaymentCard(**card_data) if card_data else None
        except Exception as e:
            logger.error(f"Error loading default card for user {user_id}: {str(e)}")
            return None

    @staticmethod
    def delete_card(card_id: str) -> Tuple[bool, str]:
        """Soft delete a payment card"""
        try:
            cards = db_service.load_all_json("payment_cards")
            card = next((c for c in cards if c.get("card_id") == card_id), None)
            
            if not card:
                return False, "Card not found"
            
            card["is_active"] = False
            card["updated_at"] = datetime.utcnow().isoformat()
            db_service.save_json("payment_cards", card_id, card)
            
            logger.info(f"Deleted payment card: {card_id}")
            return True, ""
        except Exception as e:
            error_msg = f"Error deleting card: {str(e)}"
            logger.error(error_msg)
            return False, error_msg


# Create global wallet service instance
wallet_service = WalletService()
