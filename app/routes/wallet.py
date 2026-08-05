"""
Wallet Management API Routes
Handles user wallet operations, topups, and payment card management.

Authorisation rules
-------------------
Every route here reads or moves money, so none of them are public. A `user_id` in
the path or body is a *request*, not an authorisation: user-scoped routes check
`require_self_or_admin`, and the routes that move balance without a gateway
round-trip (`/topup/confirm`, `/deduct`) are admin/internal only.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.auth import (
    caller_user_id,
    is_admin,
    require_admin,
    require_auth,
    require_self_or_admin,
)
from app.services.wallet_service import wallet_service
from app.models.wallet import (
    TopupRequest, TopupResponse, PurchaseRequest, PurchaseResponse,
    TransactionHistoryResponse
)
from app.services.payment_gateway import midtrans_service
from app.services import fx, pricing
from app.database import payment_repo
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
async def get_wallet(user_id: str, caller: dict = Depends(require_auth)):
    """
    Get wallet information for a user. Callers may only read their own.

    Args:
        user_id: User identifier

    Returns:
        Wallet information with balance and cards
    """
    require_self_or_admin(caller, user_id)
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
async def get_wallet_balance(user_id: str, caller: dict = Depends(require_auth)):
    """Get current wallet balance. Callers may only read their own."""
    require_self_or_admin(caller, user_id)
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
async def get_transactions(
    user_id: str, limit: int = 50, caller: dict = Depends(require_auth)
):
    """
    Get transaction history for user. Callers may only read their own.

    Args:
        user_id: User identifier
        limit: Number of transactions to return
        
    Returns:
        Transaction history
    """
    require_self_or_admin(caller, user_id)
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
async def initiate_topup(request: TopupRequest, caller: dict = Depends(require_auth)):
    """
    Initiate a wallet topup via Midtrans.

    The amount is carried *inside* the product id (`wallet_topup_<usd>`) so the
    payments catalogue prices it and the sum charged cannot drift from the sum
    credited. The order is written to the Postgres ledger, which is what
    `/payments/confirm` and the Midtrans webhook settle against.

    Args:
        request: Topup request with amount and optional card

    Returns:
        Midtrans payment token
    """
    target_user_id = request.user_id
    if not is_admin(caller):
        # A caller may only top up their own wallet, whatever the body claims.
        target_user_id = caller_user_id(caller)
    if not target_user_id:
        raise HTTPException(status_code=400, detail="Could not determine the paying user")

    product = f"wallet_topup_{request.amount:g}"

    try:
        await fx.ensure_fresh()
        amount_usd = pricing.resolve_price_usd(product)
        amount_idr = pricing.resolve_gross_amount_idr(product)
    except pricing.UnknownProduct as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        wallet = wallet_service.get_or_create_wallet(target_user_id)

        result = await midtrans_service.create_transaction(
            user_id=target_user_id,
            product=product,
            customer_details={},
            custom_field1=f"Wallet Topup: ${amount_usd}",
            custom_field2=f"User: {target_user_id}",
        )

        if result.get("success"):
            payment_repo.create({
                "payment_id": generate_id("payment"),
                "order_id": result["order_id"],
                "user_id": target_user_id,
                "product": product,
                "amount": amount_usd,
                "amount_idr": amount_idr,
                "usd_idr_rate": fx.current_rate(),
                "status": "pending",
                "payment_method": "midtrans",
                "is_mock": result.get("is_mock", False),
                "transaction_id": result.get("transaction_id"),
            })

            return TopupResponse(
                success=True,
                wallet_id=wallet.wallet_id,
                order_id=result["order_id"],
                token=result.get("token"),
                previous_balance=wallet.balance,
            )

        return TopupResponse(
            success=False,
            error=result.get("error", "Failed to create payment"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Topup initiation failed for %s: %s", target_user_id, e)
        raise HTTPException(status_code=500, detail="Could not start top-up")


@router.post("/topup/confirm")
async def confirm_topup(order_id: str, caller: dict = Depends(require_auth)):
    """
    Settle a top-up after the customer finishes checkout.

    Only the order id is accepted, and the balance is credited by the shared
    settlement path — which re-verifies the payment with Midtrans and applies it
    exactly once. The previous version took the amount from the request and
    credited it without any verification at all, so any caller could mint funds.
    """
    record = payment_repo.find_by_order_id(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Payment not found")
    require_self_or_admin(caller, record.get("user_id"))

    result = await midtrans_service.settle_order(
        order_id,
        expected_user_id=None if is_admin(caller) else caller_user_id(caller),
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400, detail=result.get("error", "Could not confirm top-up")
        )

    granted = bool(result.get("granted") or result.get("already_granted"))
    return {
        "success": True,
        "message": "Wallet topup confirmed" if granted else "Payment not yet settled",
        "order_id": order_id,
        "status": result.get("status"),
        "granted": granted,
        "new_balance": wallet_service.get_wallet_balance(record.get("user_id")),
    }


# ============================================================================
# Purchase Deduction Endpoint
# ============================================================================

@router.post("/deduct")
async def deduct_for_purchase(
    request: PurchaseRequest, _admin: dict = Depends(require_admin)
):
    """
    Deduct amount from wallet for purchase. Admin/internal callers only.

    Spending someone's balance is a server-side decision made by whichever
    service consumed the product, so this is not a route a browser may call —
    an open version let any caller drain any wallet.

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
async def add_payment_card(request: AddCardRequest, caller: dict = Depends(require_auth)):
    """
    Add a new payment card for wallet topup. A caller may only add their own.

    Args:
        request: Card details

    Returns:
        Saved card information
    """
    if not is_admin(caller):
        request.user_id = caller_user_id(caller) or request.user_id
    require_self_or_admin(caller, request.user_id)
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
async def get_payment_cards(user_id: str, caller: dict = Depends(require_auth)):
    """Get all saved payment cards for user. Callers may only read their own."""
    require_self_or_admin(caller, user_id)
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
async def set_default_card(card_id: str, caller: dict = Depends(require_auth)):
    """Set a payment card as default. Callers may only touch their own cards."""
    try:
        # Get card
        from app.database.db_service import DatabaseService
        db_service = DatabaseService()
        cards = db_service.load_all_json("payment_cards")
        card = next((c for c in cards if c.get("card_id") == card_id), None)

        if not card:
            raise HTTPException(status_code=404, detail="Card not found")

        user_id = card.get("user_id")
        # Ownership is checked against the stored card, not against anything the
        # caller supplied — the card id alone is not an authorisation.
        require_self_or_admin(caller, user_id)

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
async def delete_payment_card(card_id: str, caller: dict = Depends(require_auth)):
    """Delete a payment card. Callers may only delete their own."""
    try:
        from app.database.db_service import DatabaseService

        cards = DatabaseService().load_all_json("payment_cards")
        card = next((c for c in cards if c.get("card_id") == card_id), None)
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        require_self_or_admin(caller, card.get("user_id"))

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
