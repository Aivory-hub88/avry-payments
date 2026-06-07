"""
Payment Gateway API Routes
Handles Midtrans payment processing and transaction management.
"""

from fastapi import APIRouter, HTTPException, Request, Body, Depends
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime

from app.services.payment_gateway import midtrans_service
from midtrans_config import midtrans_config
from app.services.payment_validation import PaymentValidationService
from app.database.db_service import DatabaseService
from app.utils.id_generator import generate_id
from app.auth import require_admin, require_auth

# Initialize database service
db_service = DatabaseService()

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


# ============================================================================
# Pydantic Models
# ============================================================================

class CreateTransactionRequest(BaseModel):
    """Request to create a new payment transaction."""
    user_id: str = Field(..., description="User identifier")
    amount: float = Field(..., ge=1, description="Payment amount in USD")
    product: str = Field(..., description="Product being purchased")
    customer_email: Optional[str] = Field(None, description="Customer email")
    customer_first_name: Optional[str] = Field(None, description="Customer first name")
    custom_field1: Optional[str] = Field(None, description="Custom field 1")
    custom_field2: Optional[str] = Field(None, description="Custom field 2")


class CreateTransactionResponse(BaseModel):
    """Response for transaction creation."""
    success: bool
    order_id: Optional[str] = None
    token: Optional[str] = None
    redirect_url: Optional[str] = None
    transaction_id: Optional[str] = None
    error: Optional[str] = None


class TransactionStatusResponse(BaseModel):
    """Response for transaction status check."""
    success: bool
    order_id: Optional[str] = None
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None
    payment_type: Optional[str] = None
    fraud_status: Optional[str] = None
    gross_amount: Optional[str] = None
    error: Optional[str] = None


class RefundRequest(BaseModel):
    """Request to process a refund."""
    order_id: str = Field(..., description="Order ID to refund")
    amount: Optional[int] = Field(None, description="Amount to refund (full if not specified)")


class RefundResponse(BaseModel):
    """Response for refund processing."""
    success: bool
    order_id: Optional[str] = None
    refund_id: Optional[str] = None
    refund_amount: Optional[str] = None
    error: Optional[str] = None


class PaymentHistoryResponse(BaseModel):
    """Response for payment history."""
    success: bool
    payments: list[Dict[str, Any]] = []
    total: int = 0


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/midtrans/create", response_model=CreateTransactionResponse)
async def create_midtrans_transaction(request: CreateTransactionRequest):
    """
    Create a new Midtrans payment transaction.
    
    This endpoint creates a payment transaction with Midtrans and returns
    the transaction token and redirect URL for the payment flow.
    
    Args:
        request: Transaction creation request
        
    Returns:
        Transaction details including token and redirect URL
    """
    try:
        # Validate product - accept various product formats
        if not any(request.product.startswith(prefix) for prefix in ["credits_", "ai_", "foundation", "acceleration", "intelligence"]):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid product format"
            )
        
        # Build customer details
        customer_details = {}
        if request.customer_email:
            customer_details["email"] = request.customer_email
        if request.customer_first_name:
            customer_details["first_name"] = request.customer_first_name
        
        # Create transaction with Midtrans (or mock)
        result = await midtrans_service.create_transaction(
            user_id=request.user_id,
            amount=request.amount,
            product=request.product,
            customer_details=customer_details,
            custom_field1=request.custom_field1,
            custom_field2=request.custom_field2,
        )
        
        if result["success"]:
            # Save payment record to database
            payment_record = {
                "payment_id": generate_id("payment"),
                "order_id": result["order_id"],
                "user_id": request.user_id,
                "product": request.product,
                "amount": request.amount,
                "status": "pending",
                "payment_method": "midtrans",
                "is_mock": result.get("is_mock", False),  # Flag mock payments
                "transaction_id": result.get("transaction_id"),
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            db_service.save_json("payments", payment_record["payment_id"], payment_record)
        
        return CreateTransactionResponse(**result)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/midtrans/status/{order_id}", response_model=TransactionStatusResponse)
async def check_transaction_status(order_id: str):
    """
    Check the status of a Midtrans transaction.
    
    Args:
        order_id: Order ID to check
        
    Returns:
        Transaction status information
    """
    try:
        result = await midtrans_service.get_transaction_status(order_id)
        return TransactionStatusResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/midtrans/webhook")
async def midtrans_webhook(request: Request, payload: Dict[str, Any] = Body(...)):
    """
    Handle Midtrans webhook notifications.
    
    This endpoint receives payment notifications from Midtrans and updates
    the payment records accordingly.
    
    Args:
        request: HTTP request
        payload: Webhook payload from Midtrans
        
    Returns:
        Processing result
    """
    try:
        # Verify webhook signature (optional, for production)
        # In production, you should verify the signature from headers
        
        result = await midtrans_service.handle_webhook(payload)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/midtrans/refund", response_model=RefundResponse)
async def refund_payment(request: RefundRequest):
    """
    Process a refund for a payment.
    
    Args:
        request: Refund request with order_id and optional amount
        
    Returns:
        Refund result
    """
    try:
        result = await midtrans_service.refund_payment(
            order_id=request.order_id,
            amount=request.amount,
        )
        return RefundResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/admin", response_model=PaymentHistoryResponse)
async def get_all_payments_admin(_admin: dict = Depends(require_admin)):
    """
    Get all payments across all users (admin endpoint).

    Requires an admin/superadmin caller. Returns every payment transaction
    recorded by the service, sorted by creation time (most recent first).
    This is the source of truth for the admin dashboard payments view.
    """
    try:
        payments = db_service.load_all_json("payments")
        payments.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return PaymentHistoryResponse(
            success=True,
            payments=payments,
            total=len(payments),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{user_id}", response_model=PaymentHistoryResponse)
async def get_payment_history(user_id: str):
    """
    Get payment history for a user.
    
    Args:
        user_id: User identifier
        
    Returns:
        User's payment history
    """
    try:
        payments = db_service.load_all_json("payments")
        user_payments = [p for p in payments if p.get("user_id") == user_id]
        
        # Sort by created_at descending
        user_payments.sort(
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )
        
        return PaymentHistoryResponse(
            success=True,
            payments=user_payments,
            total=len(user_payments),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/record")
async def record_payment(
    user_id: str,
    amount: float,
    payment_method: str = "manual",
    product: str = "ai_blueprint",
    _admin: dict = Depends(require_admin),
):
    """
    Record a manual payment (admin endpoint).
    
    Requires an admin/superadmin caller. This endpoint allows admins to
    manually record payments that were made outside of the Midtrans system
    (e.g., bank transfer, cash).
    
    Args:
        user_id: User identifier
        amount: Payment amount
        payment_method: Method of payment
        product: Product purchased
        
    Returns:
        Recording result
    """
    try:
        payment_service = PaymentValidationService()
        success = await payment_service.record_payment(
            user_id=user_id,
            amount=amount,
            payment_method=payment_method,
            product=product,
        )
        
        if success:
            return {
                "success": True,
                "message": "Payment recorded successfully",
                "user_id": user_id,
                "amount": amount,
                "product": product,
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to record payment")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ConfirmPaymentRequest(BaseModel):
    """Request to confirm a payment."""
    order_id: str = Field(..., description="Midtrans order ID")
    user_id: str = Field(..., description="User identifier")
    product: str = Field(..., description="Product purchased")
    amount: float = Field(..., description="Amount paid")
    is_mock: bool = Field(False, description="Whether this is a mock payment")


@router.post("/confirm")
async def confirm_payment(request: ConfirmPaymentRequest):
    """
    Confirm a payment and process the purchase.
    
    This endpoint is called after successful payment to finalize the purchase,
    update user tier/features, and record the transaction.
    
    Args:
        request: Payment confirmation request
        
    Returns:
        Purchase result
    """
    try:
        from app.services.purchase_service import PurchaseService
        
        # Process the purchase (update user tier, features, credits, etc)
        success, data, error = PurchaseService.process_purchase(
            user_id=request.user_id,
            product=request.product,
            amount=request.amount,
            order_id=request.order_id,
            is_mock=request.is_mock,
        )
        
        if success:
            # Update payment record status
            payments = db_service.load_all_json("payments")
            payment = next((p for p in payments if p.get("order_id") == request.order_id), None)
            if payment:
                payment["status"] = "completed"
                payment["is_mock"] = request.is_mock
                payment["updated_at"] = datetime.utcnow().isoformat()
                db_service.save_json("payments", payment["payment_id"], payment)
            
            return {
                "success": True,
                "message": "Payment confirmed and purchase processed",
                "is_mock": request.is_mock,
                "data": data,
            }
        else:
            return {
                "success": False,
                "message": "Failed to process purchase",
                "is_mock": request.is_mock,
                "error": error,
            }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/client-key")
async def get_client_key():
    """
    Get Midtrans client key for frontend.
    
    Returns:
        Client key for Midtrans Snap integration
    """
    client_key = midtrans_service.get_client_key()
    
    if not client_key:
        raise HTTPException(
            status_code=500,
            detail="Midtrans client key not configured"
        )
    
    return {
        "success": True,
        "client_key": client_key,
        "is_production": midtrans_service.is_production,
    }


@router.get("/config")
async def get_payment_config():
    """
    Get payment configuration for frontend.

    Returns mock_mode=true when Midtrans credentials are not configured (development).
    """
    is_mock = not midtrans_config.is_configured
    return {
        "success": True,
        "is_configured": midtrans_config.is_configured,
        "is_mock": is_mock,
        "is_production": midtrans_config.is_production if not is_mock else False,
        "client_key": midtrans_config.get_client_key() or "MOCK_CLIENT_KEY",
        "products": {
            "ai_snapshot": {
                "name": "AI Snapshot",
                "price_usd": midtrans_config.snapshot_price_usd,
                "price_idr": midtrans_config.convert_usd_to_idr(midtrans_config.snapshot_price_usd),
            },
            "ai_blueprint": {
                "name": "AI System Blueprint",
                "price_usd": midtrans_config.blueprint_price_usd,
                "price_idr": midtrans_config.convert_usd_to_idr(midtrans_config.blueprint_price_usd),
            },
        },
    }


# ============================================================================
# List All Payments Endpoint (Phase 2)
# ============================================================================

@router.get("")
async def list_payments(_admin: dict = Depends(require_admin)):
    """
    List all payments (GET /api/v1/payments) - admin only
    
    Returns:
        List of payment transactions
    """
    try:
        payments = db_service.load_all_json("payments")
        payments.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {
            "success": True,
            "payments": payments,
            "total": len(payments),
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
