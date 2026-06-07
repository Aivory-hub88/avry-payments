"""
Diagnostic record models - placeholder for payment service.
In the actual diagnostic service, these would be more complex.
"""

from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime


class DiagnosticRecord(BaseModel):
    """Diagnostic record data"""
    diagnostic_id: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    company_name: Optional[str] = None
    industry: Optional[str] = None
    answers: List[Dict[str, Any]] = []
    score: int = 0
    category: str = ""
    created_at: datetime
