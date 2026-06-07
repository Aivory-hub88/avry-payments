"""
Payment validation service using database payment records.
"""

from typing import Optional
from datetime import datetime

from app.models.validation import ValidationResult
from app.database.db_service import db


class PaymentValidationService:
    """
    Validates payment status for product access by checking database payment records.
    
    All users (including super admin) validated identically - super admin has seeded payment records.
    """
    
    # Product prices
    BLUEPRINT_PRICE = 85  # $85 for Blueprint tier
    SNAPSHOT_PRICE = 29  # $29 for Snapshot tier
    FOUNDATION_PRICE = 200  # $200 for Foundation tier
    PRO_PRICE = 500  # $500 for Pro tier
    ENTERPRISE_PRICE = 1000  # $1000 for Enterprise tier
    
    # Credit prices
    CREDIT_PRICES = {
        50: 5,
        100: 9,
        250: 20,
        500: 38,
        1000: 70,
        2500: 165,
        5000: 300,
        10000: 550,
    }
    
    async def validate_blueprint_access(
        self,
        user_id: str
    ) -> ValidationResult:
        """
        Check if user can access Blueprint generation by checking payment records.
        
        Args:
            user_id: User identifier
            
        Returns:
            ValidationResult with access decision
        """
        # Check for paid blueprint record in database (either ai_blueprint or ai_bundle)
        payments = db.load_all_json("payments")
        user_blueprint_payment = next(
            (p for p in payments 
             if p.get("user_id") == user_id 
             and p.get("product") in ["ai_blueprint", "ai_bundle"] 
             and p.get("status") == "paid"),
            None
        )
        
        if user_blueprint_payment:
            return ValidationResult(
                allowed=True,
                bypass=False,
                message="Blueprint access granted - payment verified",
                payment_required=False
            )
        
        # Payment required
        return ValidationResult(
            allowed=False,
            bypass=False,
            message=f"Payment required: ${self.BLUEPRINT_PRICE} for AI Blueprint",
            payment_required=True
        )
    
    async def validate_snapshot_access(
        self,
        user_id: str
    ) -> ValidationResult:
        """
        Check if user can access Snapshot generation by checking payment records.
        
        Args:
            user_id: User identifier
            
        Returns:
            ValidationResult with access decision
        """
        # Check for paid snapshot record in database (either ai_snapshot or ai_bundle)
        payments = db.load_all_json("payments")
        user_snapshot_payment = next(
            (p for p in payments 
             if p.get("user_id") == user_id 
             and p.get("product") in ["ai_snapshot", "ai_bundle"] 
             and p.get("status") == "paid"),
            None
        )
        
        if user_snapshot_payment:
            return ValidationResult(
                allowed=True,
                bypass=False,
                message="Snapshot access granted - payment verified",
                payment_required=False
            )
        
        # Payment required
        return ValidationResult(
            allowed=False,
            bypass=False,
            message=f"Payment required: ${self.SNAPSHOT_PRICE} for AI Snapshot",
            payment_required=True
        )
    
    async def record_payment(
        self,
        user_id: str,
        amount: float,
        payment_method: str = "stripe",
        product: str = "ai_blueprint",
        order_id: Optional[str] = None,
    ) -> bool:
        """
        Record successful payment.
        
        Args:
            user_id: User identifier
            amount: Payment amount
            payment_method: Payment method used
            product: Product purchased (ai_snapshot, ai_blueprint, ai_bundle, step3_subscription)
            order_id: Optional Midtrans order ID
            
        Returns:
            True if recorded successfully
        """
        try:
            from app.utils.id_generator import generate_id
            
            payment_id = generate_id("payment")
            payment_record = {
                "payment_id": payment_id,
                "user_id": user_id,
                "product": product,
                "amount": amount,
                "status": "paid",
                "payment_method": payment_method,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            
            if order_id:
                payment_record["order_id"] = order_id
            
            db.save_json("payments", payment_id, payment_record)
            return True
            
        except Exception as e:
            print(f"Error recording payment: {e}")
            return False
    
    async def check_payment_status(
        self,
        user_id: str,
        product: str = "ai_blueprint"
    ) -> bool:
        """
        Check if user has paid for product.
        
        Args:
            user_id: User identifier
            product: Product to check (ai_snapshot, ai_blueprint, ai_bundle, step3_subscription)
            
        Returns:
            True if payment completed
        """
        payments = db.load_all_json("payments")
        return any(
            p.get("user_id") == user_id 
            and (p.get("product") == product or (product in ["ai_snapshot", "ai_blueprint"] and p.get("product") == "ai_bundle"))
            and p.get("status") == "paid"
            for p in payments
        )
    
    async def validate_access(
        self,
        user_id: str,
        product: str
    ) -> ValidationResult:
        """
        Generic access validation for any product.
        
        Args:
            user_id: User identifier
            product: Product to validate (ai_snapshot, ai_blueprint, foundation, pro, enterprise, credits_*)
            
        Returns:
            ValidationResult with access decision
        """
        payments = db.load_all_json("payments")
        
        # Check for subscription products
        subscription_products = ["foundation", "pro", "enterprise"]
        if product in subscription_products:
            user_subscription = next(
                (p for p in payments 
                 if p.get("user_id") == user_id 
                 and p.get("product") == product 
                 and p.get("status") == "paid"),
                None
            )
            if user_subscription:
                return ValidationResult(
                    allowed=True,
                    bypass=False,
                    message=f"{product.capitalize()} access granted - payment verified",
                    payment_required=False
                )
            price = self._get_price(product)
            return ValidationResult(
                allowed=False,
                bypass=False,
                message=f"Payment required: ${price} for {product.capitalize()} plan",
                payment_required=True
            )
        
        # Check for one-time products
        if product in ["ai_snapshot", "ai_blueprint"]:
            return await self._validate_one_time_product(user_id, product)
        
        # Check for credits
        if product.startswith("credits_"):
            return await self._validate_credits(user_id, product)
        
        return ValidationResult(
            allowed=False,
            bypass=False,
            message="Product not found",
            payment_required=True
        )
    
    async def _validate_one_time_product(self, user_id: str, product: str) -> ValidationResult:
        """Validate one-time purchase products."""
        payments = db.load_all_json("payments")
        user_payment = next(
            (p for p in payments 
             if p.get("user_id") == user_id 
             and p.get("product") == product 
             and p.get("status") == "paid"),
            None
        )
        
        if user_payment:
            return ValidationResult(
                allowed=True,
                bypass=False,
                message=f"{product} access granted - payment verified",
                payment_required=False
            )
        
        price = self.SNAPSHOT_PRICE if product == "ai_snapshot" else self.BLUEPRINT_PRICE
        return ValidationResult(
            allowed=False,
            bypass=False,
            message=f"Payment required: ${price} for {product}",
            payment_required=True
        )
    
    async def _validate_credits(self, user_id: str, product: str) -> ValidationResult:
        """Validate credit top-up products."""
        payments = db.load_all_json("payments")
        credit_amount = int(product.split("_")[1])
        
        user_credits = sum(
            p.get("amount", 0) for p in payments 
            if p.get("user_id") == user_id 
            and p.get("product") == product 
            and p.get("status") == "paid"
        )
        
        if user_credits >= credit_amount:
            return ValidationResult(
                allowed=True,
                bypass=False,
                message=f"{credit_amount} credits verified",
                payment_required=False
            )
        
        price = self.CREDIT_PRICES.get(credit_amount, 0)
        return ValidationResult(
            allowed=False,
            bypass=False,
            message=f"Payment required: ${price} for {credit_amount} credits",
            payment_required=True
        )
    
    def _get_price(self, product: str) -> int:
        """Get price for any product."""
        prices = {
            "ai_snapshot": self.SNAPSHOT_PRICE,
            "ai_blueprint": self.BLUEPRINT_PRICE,
            "foundation": self.FOUNDATION_PRICE,
            "pro": self.PRO_PRICE,
            "enterprise": self.ENTERPRISE_PRICE,
        }
        return prices.get(product, 0)
