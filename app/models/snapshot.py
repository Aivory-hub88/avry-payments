"""
Snapshot record models - placeholder for payment service.
In the actual diagnostic service, these would be more complex.
"""

from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime


class SnapshotRecord(BaseModel):
    """Snapshot record data"""
    snapshot_id: str
    diagnostic_id: Optional[str] = None
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    company_name: Optional[str] = None
    industry: Optional[str] = None
    answers: List[Dict[str, Any]] = []
    readiness_score: int = 0
    category_scores: Dict[str, float] = {}
    primary_objective: str = ""
    top_recommendations: List[str] = []
    pain_points: List[str] = []
    workflows: List[str] = []
    key_processes: List[str] = []
    automation_level: str = ""
    data_quality_score: int = 0
    created_at: datetime
