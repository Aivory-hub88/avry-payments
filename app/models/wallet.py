"""
Wallet model for user account funds and transactions.
Implements secure wallet management with transaction tracking.
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime
from enum import Enum


class TransactionType(str, Enum):
    """Transaction types"""
    TOPUP = "topup"  # Money added via Midtrans
    PURCHASE = "purchase"  # Money deducted for purchase
    REFUND = "refund"  # Money returned from refund
    ADJUSTMENT = "adjustment"  # Admin adjustment


class TransactionStatus(str, Enum):
    """Transaction status"""
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WalletTransaction(BaseModel):
    """Individual transaction record"""
    model_config = ConfigDict(ser_json_timedelta='float')
    
    transaction_id: str
    user_id: str
    type: TransactionType
    amount: float
    balance_before: float
    balance_after: float
    description: str
    reference_id: Optional[str] = None  # Order ID or purchase ID
    status: TransactionStatus = TransactionStatus.COMPLETED
    created_at: datetime
    updated_at: datetime
    metadata: Optional[dict] = None  # Additional data (product, etc)


class Wallet(BaseModel):
    """User wallet model"""
    model_config = ConfigDict(ser_json_timedelta='float')
    
    wallet_id: str
    user_id: str
    balance: float = 0.0  # Available balance for purchases
    total_topup: float = 0.0  # Total money added (tracking)
    total_spent: float = 0.0  # Total money spent (tracking)
    total_refunded: float = 0.0  # Total refunded (tracking)
    currency: str = "USD"
    is_active: bool = True
    created_at: datetime
    updated_at: datetime
    last_transaction_at: Optional[datetime] = None


class PaymentCard(BaseModel):
    """Saved payment card for Midtrans"""
    model_config = ConfigDict(ser_json_timedelta='float')
    
    card_id: str
    user_id: str
    token: str  # Midtrans token ID
    brand: str  # visa, mastercard, amex
    last_four: str
    holder_name: str
    expiry_month: int
    expiry_year: int
    is_default: bool = False
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class WalletResponse(BaseModel):
    """Response model for wallet data"""
    wallet_id: str
    balance: float
    total_topup: float
    total_spent: float
    currency: str
    cards: List[dict] = []
    last_transaction: Optional[dict] = None


class TopupRequest(BaseModel):
    """Request to topup wallet"""
    user_id: str = Field(..., description="User ID")
    amount: float = Field(..., gt=0, description="Amount to topup in USD")
    card_id: Optional[str] = Field(None, description="Saved card ID, if None will use default")


class TopupResponse(BaseModel):
    """Response for topup request"""
    success: bool
    wallet_id: Optional[str] = None
    transaction_id: Optional[str] = None
    previous_balance: Optional[float] = None
    new_balance: Optional[float] = None
    order_id: Optional[str] = None  # Midtrans order ID
    token: Optional[str] = None  # Midtrans token
    error: Optional[str] = None


class PurchaseRequest(BaseModel):
    """Request to deduct from wallet for purchase"""
    user_id: str = Field(..., description="User ID")
    amount: float = Field(..., gt=0, description="Amount to deduct")
    product: str = Field(..., description="Product being purchased")
    reference_id: Optional[str] = Field(None, description="Order/Purchase ID")


class PurchaseResponse(BaseModel):
    """Response for purchase deduction"""
    success: bool
    transaction_id: Optional[str] = None
    previous_balance: Optional[float] = None
    new_balance: Optional[float] = None
    requires_topup: bool = False  # True if balance insufficient
    error: Optional[str] = None


class TransactionHistoryResponse(BaseModel):
    """Response for transaction history"""
    success: bool
    transactions: List[dict] = []
    total: int = 0
    balance: float = 0.0
