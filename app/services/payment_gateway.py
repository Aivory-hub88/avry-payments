"""
Midtrans Payment Gateway Service
Handles payment processing, transaction management, and webhook handling.
"""

import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class MidtransConfig:
    """Midtrans configuration from settings"""
    def __init__(self):
        self.server_key = settings.midtrans_server_key
        self.client_key = settings.midtrans_client_key
        self.is_production = settings.midtrans_is_production
        self.api_url = "https://api.sandbox.midtrans.com" if not self.is_production else "https://api.midtrans.com"
        self.snap_url = "https://app.sandbox.midtrans.com" if not self.is_production else "https://app.midtrans.com"
    
    @staticmethod
    def convert_usd_to_idr(usd_amount: float) -> int:
        """Convert USD to IDR (using approximate 1 USD = 15000 IDR)"""
        return int(usd_amount * 15000)


midtrans_config = MidtransConfig()


@dataclass
class PaymentTransaction:
    """Payment transaction data structure."""
    transaction_id: str
    order_id: str
    user_id: str
    amount: float
    product: str
    payment_type: str
    status: str  # pending, settlement, capture, deny, expire, cancel
    created_at: datetime
    updated_at: datetime


class MidtransPaymentService:
    """
    Midtrans Payment Gateway Service
    
    Handles:
    - Transaction creation
    - Payment verification
    - Webhook processing
    - Refund processing
    """
    
    def __init__(self):
        self.server_key = midtrans_config.server_key
        self.client_key = midtrans_config.client_key
        self.is_production = midtrans_config.is_production
        
        # Midtrans API URLs
        self.api_url = midtrans_config.api_url
        self.snap_url = midtrans_config.snap_url
        
        # Enable mock mode for development if keys are not configured
        self.mock_mode = not self.server_key or self.server_key == "demo_key"
        
        # Validate configuration
        if not self.server_key:
            logger.warning("MIDTRANS_SERVER_KEY not configured - using MOCK mode for development")
        if not self.client_key:
            logger.warning("MIDTRANS_CLIENT_KEY not configured - frontend checkout will use mock mode")
        
        mode_str = "MOCK (Development)" if self.mock_mode else ("PRODUCTION" if self.is_production else "SANDBOX")
        logger.info(f"Midtrans initialized: {mode_str}")
    
    def _get_headers(self) -> Dict[str, str]:
        """Get authentication headers for Midtrans API."""
        return {
            "Authorization": f"Basic {self._encode_basic_auth()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    
    def _encode_basic_auth(self) -> str:
        """Encode server key for Basic Auth (ServerKey: format per Midtrans docs)."""
        import base64
        return base64.b64encode(f"{self.server_key}:".encode()).decode() if self.server_key else ""
    
    async def create_transaction(
        self,
        user_id: str,
        amount: float,
        product: str,
        customer_details: Optional[Dict[str, Any]] = None,
        custom_field1: Optional[str] = None,
        custom_field2: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new Midtrans transaction.
        
        Args:
            user_id: User identifier
            amount: Payment amount in IDR (minimum 1000 IDR)
            product: Product being purchased (ai_snapshot, ai_blueprint, subscription)
            customer_details: Optional customer information
            custom_field1: Optional custom field for additional data
            custom_field2: Optional custom field for additional data
            
        Returns:
            Transaction response from Midtrans
        """
        # Generate order ID
        from app.utils.id_generator import generate_id
        order_id = generate_id(f"payment_{product}")
        
        # Convert amount to IDR if needed (assuming USD input)
        amount_idr = midtrans_config.convert_usd_to_idr(amount)
        
        # Build transaction request
        transaction_data = {
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount_idr,
            },
            "item_details": [{
                "id": product,
                "price": amount_idr,
                "quantity": 1,
                "name": self._get_product_name(product),
            }],
            "customer_details": customer_details or {
                "first_name": "User",
                "email": f"{user_id}@aivory.id",
                "phone": "081234567890",
            },
        }
        
        # Add custom fields if provided
        if custom_field1:
            transaction_data["custom_field1"] = custom_field1
        if custom_field2:
            transaction_data["custom_field2"] = custom_field2
        
        # Use mock mode if Midtrans is not configured
        if self.mock_mode:
            logger.info(f"[MOCK] Transaction created: {order_id} - amount: ${amount}")
            return {
                "success": True,
                "is_mock": True,
                "order_id": order_id,
                "token": f"mock_token_{order_id}",
                "redirect_url": f"https://app.sandbox.midtrans.com/snap/v1/redirection/{order_id}",
                "transaction_id": f"mock_txn_{order_id}",
            }
        
        if not self.server_key:
            raise ValueError("Midtrans server key not configured")
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.snap_url}/snap/v1/transactions",
                    json=transaction_data,
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"Transaction created: {order_id} - {result.get('token')}")
                
                return {
                    "success": True,
                    "is_mock": False,
                    "order_id": order_id,
                    "token": result.get("token"),
                    "redirect_url": result.get("redirect_url"),
                    "transaction_id": result.get("transaction_id"),
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"Midtrans API error: {e.response.status_code} - {e.response.text}")
            return {
                "success": False,
                "is_mock": False,
                "error": f"API error: {e.response.status_code}",
                "details": e.response.text,
            }
        except Exception as e:
            logger.error(f"Error creating transaction: {e}")
            return {
                "success": False,
                "is_mock": False,
                "error": str(e),
            }
    
    async def get_transaction_status(self, order_id: str) -> Dict[str, Any]:
        """
        Check transaction status from Midtrans.
        
        Args:
            order_id: Order ID to check
            
        Returns:
            Transaction status information
        """
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
                logger.info(f"Transaction status: {order_id} - {result.get('transaction_status')}")
                
                return {
                    "success": True,
                    "order_id": order_id,
                    "transaction_id": result.get("transaction_id"),
                    "transaction_status": result.get("transaction_status"),
                    "payment_type": result.get("payment_type"),
                    "fraud_status": result.get("fraud_status"),
                    "gross_amount": result.get("gross_amount"),
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"Error getting transaction status: {e.response.status_code}")
            return {
                "success": False,
                "error": f"API error: {e.response.status_code}",
            }
        except Exception as e:
            logger.error(f"Error checking transaction status: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle Midtrans webhook notifications.
        
        Args:
            payload: Webhook payload from Midtrans
            
        Returns:
            Processing result
        """
        order_id = payload.get("order_id", "")
        transaction_status = payload.get("transaction_status", "")
        fraud_status = payload.get("fraud_status", "")
        payment_type = payload.get("payment_type", "")
        
        logger.info(f"Webhook received: {order_id} - {transaction_status} - {fraud_status}")
        
        # Update payment record in database
        from app.database.db_service import db
        from app.services.payment_validation import PaymentValidationService
        
        payment_service = PaymentValidationService()
        
        # Determine if payment is valid
        is_valid_payment = transaction_status in ["settlement", "capture"] and fraud_status != "deny"
        
        # Update payment record
        try:
            # Find existing payment record
            payments = db.load_all_json("payments")
            payment_record = next(
                (p for p in payments if p.get("order_id") == order_id),
                None
            )
            
            if payment_record:
                # Update payment status
                payment_record["transaction_status"] = transaction_status
                payment_record["fraud_status"] = fraud_status
                payment_record["payment_type"] = payment_type
                payment_record["updated_at"] = datetime.utcnow().isoformat()
                
                if is_valid_payment:
                    payment_record["status"] = "paid"
                    # Grant product access
                    product = payment_record.get("product", "ai_blueprint")
                    await payment_service.record_payment(
                        user_id=payment_record.get("user_id"),
                        amount=float(payment_record.get("amount", 0)),
                        payment_method="midtrans",
                        product=product,
                    )
                
                # Save updated record
                db.save_json("payments", payment_record.get("payment_id"), payment_record)
                
                logger.info(f"Payment updated: {order_id} - Status: {transaction_status}")
                
                return {
                    "success": True,
                    "message": "Webhook processed",
                    "order_id": order_id,
                    "status": transaction_status,
                }
                
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            return {
                "success": False,
                "error": str(e),
            }
        
        return {
            "success": True,
            "message": "Webhook received (no action needed)",
            "order_id": order_id,
        }
    
    async def refund_payment(self, order_id: str, amount: Optional[int] = None) -> Dict[str, Any]:
        """
        Process a refund for a payment.
        
        Args:
            order_id: Order ID to refund
            amount: Optional amount to refund (full refund if not specified)
            
        Returns:
            Refund result
        """
        if not self.server_key:
            raise ValueError("Midtrans server key not configured")
        
        try:
            refund_data = {}
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
                logger.info(f"Refund processed: {order_id} - {result}")
                
                return {
                    "success": True,
                    "order_id": order_id,
                    "refund_id": result.get("refund_id"),
                    "refund_amount": result.get("refund_amount"),
                }
                
        except httpx.HTTPStatusError as e:
            logger.error(f"Refund API error: {e.response.status_code}")
            return {
                "success": False,
                "error": f"API error: {e.response.status_code}",
            }
        except Exception as e:
            logger.error(f"Error processing refund: {e}")
            return {
                "success": False,
                "error": str(e),
            }
    
    def _get_product_name(self, product: str) -> str:
        """Get human-readable product name."""
        product_names = {
            "ai_snapshot": "AI Snapshot",
            "ai_blueprint": "AI System Blueprint",
            "foundation": "Foundation Plan",
            "pro": "Pro Plan",
            "enterprise": "Enterprise Plan",
            "credits_50": "50 Credits",
            "credits_100": "100 Credits",
            "credits_250": "250 Credits",
            "credits_500": "500 Credits",
            "credits_1000": "1000 Credits",
            "credits_2500": "2500 Credits",
            "credits_5000": "5000 Credits",
            "credits_10000": "10000 Credits",
        }
        return product_names.get(product, product)
    
    def get_client_key(self) -> Optional[str]:
        """Get Midtrans client key for frontend."""
        return self.client_key
    
    def is_configured(self) -> bool:
        """Check if Midtrans is properly configured."""
        return bool(self.server_key and self.client_key)


# Global service instance
midtrans_service = MidtransPaymentService()
