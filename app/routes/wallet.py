"""
Wallet Management API Routes
Handles user wallet operations, topups, and payment card management.
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.services.wallet_service import wallet_service
from app.models.wallet import (
    TopupRequest, TopupResponse, PurchaseRequest, PurchaseResponse,
    TransactionHistoryResponse
)
from app.services.payment_gateway import midtrans_service
from app.database.db_service import DatabaseService
from app.utils.id_generator import generate_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wallet", tags=["wallet"])

# Initialize database service
db_service = DatabaseService()

# ============================================================================
# Pydantic Models for Requests
# ============================================================================

class AddCardRequest(BaseModel):
    """Request to add payment card"""
    user_id: str = Field(..., description="User ID")
    card_number: str = Field(..., description="Card number")
    holder_name: str = Field(..., description="Cardholder name")
    expiry_month: int = Field(..., ge=1, le=12, description="Expiry month")
    expiry_year: int = Field(..., ge=2024, le=2050, description="Expiry year")
    cvv: str = Field(..., description="Card CVV")
    is_default: bool = Field(False, description="Set as default card")


class SetDefaultCardRequest(BaseModel):
    """Request to set default card"""
    card_id: str = Field(..., description="Card ID")


# ============================================================================
# Wallet Information Endpoints
# ============================================================================

@router.get("/{user_id}")
async def get_wallet(user_id: str):
    """
    Get wallet information for a user.
    
    Args:
        user_id: User identifier
        
    Returns:
        Wallet information with balance and cards
    """
    try:
        try:
            wallet = wallet_service.get_or_create_wallet(user_id)
        except Exception as e:
            # If wallet creation fails, return empty wallet
            logger.error(f"Error getting wallet for {user_id}: {str(e)}")
            return {
                "success": True,
                "wallet_id": "",
                "balance": 0,
                "total_topup": 0,
                "total_spent": 0,
                "total_refunded": 0,
                "currency": "USD",
                "cards": [],
                "last_transaction_at": None,
            }
        
        try:
            cards = wallet_service.get_payment_cards(user_id)
        except Exception as e:
            logger.error(f"Error getting payment cards for {user_id}: {str(e)}")
            cards = []
        
        return {
            "success": True,
            "wallet_id": wallet.wallet_id,
            "balance": wallet.balance,
            "total_topup": wallet.total_topup,
            "total_spent": wallet.total_spent,
            "total_refunded": wallet.total_refunded,
            "currency": wallet.currency,
            "cards": [
                {
                    "card_id": c.card_id,
                    "brand": c.brand,
                    "lastFour": c.last_four,
                    "holderName": c.holder_name,
                    "expiryMonth": c.expiry_month,
                    "expiryYear": c.expiry_year,
                    "isDefault": c.is_default,
                }
                for c in cards
            ],
            "last_transaction_at": wallet.last_transaction_at,
        }
    except Exception as e:
        logger.error(f"Wallet endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/balance")
async def get_wallet_balance(user_id: str):
    """Get current wallet balance"""
    try:
        balance = wallet_service.get_wallet_balance(user_id)
        return {
            "success": True,
            "user_id": user_id,
            "balance": balance,
            "currency": "USD",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/transactions")
async def get_transactions(user_id: str, limit: int = 50):
    """
    Get transaction history for user.
    
    Args:
        user_id: User identifier
        limit: Number of transactions to return
        
    Returns:
        Transaction history
    """
    try:
        transactions = wallet_service.get_transaction_history(user_id, limit)
        
        return TransactionHistoryResponse(
            success=True,
            transactions=transactions,
            total=len(transactions),
            balance=wallet_service.get_wallet_balance(user_id),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Topup Endpoints
# ============================================================================

@router.post("/topup/initiate")
async def initiate_topup(request: TopupRequest):
    """
    Initiate a wallet topup via Midtrans.
    
    Args:
        request: Topup request with amount and optional card
        
    Returns:
        Midtrans payment token
    """
    try:
        # Validate user has wallet
        wallet = wallet_service.get_or_create_wallet(request.user_id)
        
        # Create transaction with Midtrans
        # This is similar to credit purchase but for wallet topup
        order_id = generate_id("topup")
        
        result = await midtrans_service.create_transaction(
            user_id=request.user_id,
            amount=request.amount,
            product=f"wallet_topup_{request.amount}",
            customer_details={},
            custom_field1=f"Wallet Topup: ${request.amount}",
            custom_field2=f"Order: {order_id}",
        )
        
        if result["success"]:
            # Save payment record for tracking and confirmation
            payment_record = {
                "payment_id": generate_id("payment"),
                "order_id": order_id,
                "user_id": request.user_id,
                "product": "wallet_topup",
                "amount": request.amount,
                "status": "pending",
                "payment_method": "midtrans",
                "is_mock": result.get("is_mock", False),
                "transaction_id": result.get("transaction_id"),
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            db_service.save_json("payments", payment_record["payment_id"], payment_record)
            
            return TopupResponse(
                success=True,
                wallet_id=wallet.wallet_id,
                order_id=order_id,
                token=result.get("token"),
                previous_balance=wallet.balance,
            )
        else:
            return TopupResponse(
                success=False,
                error=result.get("error", "Failed to create payment"),
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/topup/confirm")
async def confirm_topup(order_id: str, amount: float):
    """
    Confirm topup after successful Midtrans payment.
    Called by webhook or after payment verification.
    
    Args:
        order_id: Midtrans order ID
        amount: Topup amount
        
    Returns:
        Updated wallet information
    """
    try:
        # Get user from payment record
        from app.database.db_service import DatabaseService
        db_service = DatabaseService()
        payments = db_service.load_all_json("payments")
        payment = next((p for p in payments if p.get("order_id") == order_id), None)
        
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        
        user_id = payment.get("user_id")
        
        # Add funds to wallet
        success, wallet_info, error = wallet_service.topup_wallet(
            user_id=user_id,
            amount=amount,
            order_id=order_id,
        )
        
        if success:
            # Update payment status to completed
            payment["status"] = "completed"
            payment["updated_at"] = datetime.utcnow().isoformat()
            db_service.save_json("payments", payment.get("payment_id"), payment)
            
            return {
                "success": True,
                "message": "Wallet topup confirmed",
                "wallet_id": wallet_info.get("wallet_id"),
                "transaction_id": wallet_info.get("transaction_id"),
                "new_balance": wallet_info.get("balance"),
                "amount_added": amount,
                "order_id": order_id,
            }
        else:
            raise HTTPException(status_code=400, detail=error)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Purchase Deduction Endpoint
# ============================================================================

@router.post("/deduct")
async def deduct_for_purchase(request: PurchaseRequest):
    """
    Deduct amount from wallet for purchase.
    
    Args:
        request: Purchase request with product and amount
        
    Returns:
        Transaction result
    """
    try:
        # Check balance
        balance = wallet_service.get_wallet_balance(request.user_id)
        
        if balance < request.amount:
            return PurchaseResponse(
                success=False,
                requires_topup=True,
                error=f"Insufficient balance. Current: ${balance}, Required: ${request.amount}",
            )
        
        # Deduct from wallet
        success, txn_info, error = wallet_service.deduct_for_purchase(
            user_id=request.user_id,
            amount=request.amount,
            product=request.product,
            order_id=request.reference_id or generate_id("purchase"),
        )
        
        if success:
            return PurchaseResponse(
                success=True,
                transaction_id=txn_info.get("transaction_id"),
                previous_balance=txn_info.get("balance_before"),
                new_balance=txn_info.get("balance_after"),
            )
        else:
            return PurchaseResponse(
                success=False,
                requires_topup=True,
                error=error,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Payment Card Management
# ============================================================================

@router.post("/cards/add")
async def add_payment_card(request: AddCardRequest):
    """
    Add a new payment card for wallet topup.
    
    Args:
        request: Card details
        
    Returns:
        Saved card information
    """
    try:
        # In production, tokenize card with Midtrans
        # For now, create a placeholder token
        card_token = generate_id("card_token")
        
        success, card, error = wallet_service.save_payment_card(
            user_id=request.user_id,
            token=card_token,
            brand=request.card_number[:1],  # Simplified - use actual BIN detection
            last_four=request.card_number[-4:],
            holder_name=request.holder_name,
            expiry_month=request.expiry_month,
            expiry_year=request.expiry_year,
            is_default=request.is_default,
        )
        
        if success:
            return {
                "success": True,
                "card_id": card.card_id,
                "brand": card.brand,
                "lastFour": card.last_four,
                "holderName": card.holder_name,
                "isDefault": card.is_default,
            }
        else:
            raise HTTPException(status_code=400, detail=error)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/cards")
async def get_payment_cards(user_id: str):
    """Get all saved payment cards for user"""
    try:
        cards = wallet_service.get_payment_cards(user_id)
        
        return {
            "success": True,
            "cards": [
                {
                    "card_id": c.card_id,
                    "brand": c.brand,
                    "lastFour": c.last_four,
                    "holderName": c.holder_name,
                    "expiryMonth": c.expiry_month,
                    "expiryYear": c.expiry_year,
                    "isDefault": c.is_default,
                }
                for c in cards
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/cards/{card_id}/default")
async def set_default_card(card_id: str):
    """Set a payment card as default"""
    try:
        # Get card
        from app.database.db_service import DatabaseService
        db_service = DatabaseService()
        cards = db_service.load_all_json("payment_cards")
        card = next((c for c in cards if c.get("card_id") == card_id), None)
        
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        
        user_id = card.get("user_id")
        
        # Unset other defaults
        for c in cards:
            if c.get("user_id") == user_id and c.get("is_default"):
                c["is_default"] = False
                db_service.save_json("payment_cards", c.get("card_id"), c)
        
        # Set this as default
        card["is_default"] = True
        card["updated_at"] = datetime.utcnow().isoformat()
        db_service.save_json("payment_cards", card_id, card)
        
        return {
            "success": True,
            "message": "Card set as default",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/cards/{card_id}")
async def delete_payment_card(card_id: str):
    """Delete a payment card"""
    try:
        success, error = wallet_service.delete_card(card_id)
        
        if success:
            return {
                "success": True,
                "message": "Card deleted successfully",
            }
        else:
            raise HTTPException(status_code=400, detail=error)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
