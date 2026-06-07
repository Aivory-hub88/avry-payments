"""
Credit Manager Service - Handles intelligence credit operations
"""

import logging
from typing import Dict
from datetime import datetime

logger = logging.getLogger(__name__)

class CreditManager:
    """
    Manages intelligence credits for console operations.
    Tracks balances, deductions, and transaction history.
    """
    
    def __init__(self):
        # Mock credit storage (in production, use database)
        self.credits = {
            "demo_user": 50  # Default credits
        }
        self.transactions = []
    
    def get_credits(self, user_id: str) -> int:
        """Get current credit balance for user."""
        return self.credits.get(user_id, 0)
    
    def deduct_credits(self, user_id: str, amount: int, operation: str) -> int:
        """
        Deduct credits from user balance.
        
        Args:
            user_id: User identifier
            amount: Credits to deduct
            operation: Operation type (for audit)
            
        Returns:
            New credit balance
        """
        current = self.get_credits(user_id)
        
        if current < amount:
            raise ValueError(f"Insufficient credits. Need {amount}, have {current}")
        
        new_balance = current - amount
        self.credits[user_id] = new_balance
        
        # Log transaction
        self.transactions.append({
            "user_id": user_id,
            "operation": operation,
            "amount": amount,
            "balance_before": current,
            "balance_after": new_balance,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        logger.info(f"Deducted {amount} credits from {user_id} for {operation}. New balance: {new_balance}")
        
        return new_balance
    
    def add_credits(self, user_id: str, amount: int, reason: str = "manual_add") -> int:
        """Add credits to user balance."""
        current = self.get_credits(user_id)
        new_balance = current + amount
        self.credits[user_id] = new_balance
        
        self.transactions.append({
            "user_id": user_id,
            "operation": reason,
            "amount": amount,
            "balance_before": current,
            "balance_after": new_balance,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        logger.info(f"Added {amount} credits to {user_id}. New balance: {new_balance}")
        
        return new_balance
    
    def estimate_cost(self, operation: str, tier: str, **kwargs) -> int:
        """
        Estimate credit cost for an operation.
        
        Args:
            operation: Operation type
            tier: User tier
            **kwargs: Additional parameters (e.g., page_count for documents)
            
        Returns:
            Estimated credit cost
        """
        costs = {
            "ai_message": 1,
            "workflow_blueprint": 8,
            "workflow_agentic": 15,
            "workflow_enterprise": 25,
            "workflow_trigger": {"builder": 2, "operator": 3, "enterprise": 5},
            "log_analysis": {"builder": 3, "operator": 6, "enterprise": 12},
            "workflow_modification": {"operator": 6, "enterprise": 10},
            "diagnostic_insight": 2,
            "diagnostic_deep": 5
        }
        
        # Handle tier-specific costs
        if operation in ["workflow_trigger", "log_analysis", "workflow_modification"]:
            return costs[operation].get(tier, 1)
        
        # Handle document parsing (page-based)
        if operation == "document_parsing":
            pages = kwargs.get("page_count", 1)
            if pages < 5:
                return 3
            elif pages < 20:
                return 8
            else:
                return 15
        
        return costs.get(operation, 1)
    
    def get_transaction_history(self, user_id: str, limit: int = 10) -> list:
        """Get recent transaction history for user."""
        user_transactions = [t for t in self.transactions if t['user_id'] == user_id]
        return sorted(user_transactions, key=lambda x: x['timestamp'], reverse=True)[:limit]
