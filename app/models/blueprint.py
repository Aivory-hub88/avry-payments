"""
Blueprint data models for AI System Blueprint ($79) generation pipeline.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Literal
from datetime import datetime
from enum import Enum


class AgentDefinition(BaseModel):
    """Definition of an AI agent in the blueprint."""
    id: str = Field(pattern=r"^agent_\d{2}$", description="Unique agent ID (agent_01, agent_02, ...)")
    name: str = Field(min_length=3, max_length=100, description="Descriptive agent name")
    trigger: Literal["schedule", "webhook", "event", "manual"] = Field(description="Agent activation trigger")
    tools: List[str] = Field(min_items=1, description="Required capabilities/tools")
    pseudo_logic: List[str] = Field(min_items=1, max_items=10, description="IF/ELSE/THEN conditional statements")


class WorkflowDefinition(BaseModel):
    """Definition of a workflow connecting multiple agents."""
    id: str = Field(pattern=r"^workflow_\d{2}$", description="Unique workflow ID")
    name: str = Field(min_length=3, max_length=100, description="Workflow name")
    agents: List[str] = Field(min_items=1, description="Agent IDs involved in workflow")
    description: str = Field(min_length=10, max_length=500, description="Workflow description")


class IntegrationRequirement(BaseModel):
    """External service integration requirement."""
    service_name: str = Field(description="Name of external service")
    integration_type: Literal["API", "Webhook", "Database", "File"] = Field(description="Integration method")
    priority: Literal["high", "medium", "low"] = Field(description="Integration priority")
    reason: str = Field(description="Why this integration is needed")


class BlueprintJSON(BaseModel):
    """Complete Blueprint JSON structure."""
    blueprint_id: str = Field(pattern=r"^bp_[a-z0-9]+$", description="Unique blueprint identifier")
    version: str = Field(default="1.0", description="Blueprint version")
    system_name: str = Field(min_length=5, max_length=100, description="AI system name")
    generated_for: EmailStr = Field(description="User email")
    generated_at: datetime = Field(default_factory=datetime.utcnow, description="Generation timestamp")
    snapshot_id: str = Field(description="Source AI Snapshot ID")
    agents: List[AgentDefinition] = Field(min_items=2, max_items=5, description="Agent definitions")
    workflows: List[WorkflowDefinition] = Field(min_items=1, description="Workflow definitions")
    integrations_required: List[IntegrationRequirement] = Field(description="Required integrations")
    deployment_estimate: str = Field(pattern=r"^\d+-\d+ hours$", description="Deployment time estimate")
    schema_version: Literal["aivory-v1"] = Field(default="aivory-v1", description="Blueprint schema version")


class BlueprintMetadata(BaseModel):
    """Metadata for stored blueprint."""
    blueprint_id: str = Field(description="Unique blueprint identifier")
    user_id: str = Field(description="Owner user ID")
    schema_version: str = Field(description="Blueprint schema version")
    system_name: str = Field(description="AI system name")
    created_at: datetime = Field(description="Creation timestamp")
    version: str = Field(description="Blueprint version")
    json_path: str = Field(description="Path to JSON file")
    pdf_path: str = Field(description="Path to PDF file")
    snapshot_id: str = Field(description="Source snapshot ID")


class BlueprintGenerationRequest(BaseModel):
    """Request to generate a blueprint."""
    user_id: str = Field(description="User identifier")
    snapshot_id: str = Field(description="AI Snapshot identifier")


class BlueprintGenerationResult(BaseModel):
    """Result of blueprint generation."""
    success: bool = Field(description="Generation success status")
    blueprint_id: str = Field(description="Generated blueprint ID")
    json_url: str = Field(description="URL to download JSON")
    pdf_url: str = Field(description="URL to download PDF")
    message: str = Field(description="Status message")


class UploadResult(BaseModel):
    """Result of blueprint PDF upload to AI Console."""
    success: bool = Field(description="Upload success status")
    blueprint_id: Optional[str] = Field(description="Extracted blueprint ID")
    schema_type: Literal["aivory-v1", "external-known", "external-unknown"] = Field(description="Detected schema type")
    message: str = Field(description="Status message")
    translation_status: Literal["ready", "processing", "manual-required"] = Field(description="Translation readiness")


class ValidationResult(BaseModel):
    """Result of payment/access validation."""
    allowed: bool = Field(description="Access allowed")
    bypass: bool = Field(default=False, description="Super admin bypass used")
    message: str = Field(description="Validation message")
    payment_required: bool = Field(default=False, description="Payment required")


class SchemaType(str, Enum):
    """Blueprint schema types for AI Console routing."""
    AIVORY_V1 = "aivory-v1"
    EXTERNAL_KNOWN = "external-known"
    EXTERNAL_UNKNOWN = "external-unknown"


class SnapshotData(BaseModel):
    """AI Snapshot data structure (from $15 tier)."""
    snapshot_id: str = Field(description="Snapshot identifier")
    user_email: EmailStr = Field(description="User email")
    company_name: str = Field(description="Company name")
    readiness_score: int = Field(ge=0, le=100, description="AI readiness score")
    primary_objective: str = Field(description="Primary business objective")
    industry: Optional[str] = Field(description="Industry sector")
    key_processes: List[str] = Field(description="Key business processes")
    automation_level: str = Field(description="Current automation level")
    pain_points: List[str] = Field(description="Business pain points")
    workflows: List[str] = Field(description="Existing workflows")
    data_quality_score: int = Field(ge=0, le=100, description="Data quality score")


class StorageResult(BaseModel):
    """Result of blueprint storage operation."""
    success: bool = Field(description="Storage success status")
    blueprint_id: str = Field(description="Blueprint identifier")
    json_url: str = Field(description="JSON file URL")
    pdf_url: str = Field(description="PDF file URL")
    message: str = Field(description="Status message")
