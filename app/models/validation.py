"""
Validation models for payment and access control.
"""

from pydantic import BaseModel
from typing import Optional


class ValidationResult(BaseModel):
    """Result of a validation check"""
    allowed: bool
    bypass: bool = False
    message: str
    payment_required: bool = False
    required_amount: Optional[float] = None
