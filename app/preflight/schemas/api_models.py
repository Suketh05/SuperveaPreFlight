"""Pydantic models for the public Preflight API surface.

These are the models exchanged over HTTP at POST /api/v1/preflight and
consumed internally by the existing OpenAI-compatible endpoint wiring.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "system", "assistant"]
    content: str


class PreflightConstraints(BaseModel):
    max_cost_usd: Optional[float] = Field(default=None, ge=0)
    max_latency_ms: Optional[int] = Field(default=None, ge=0)
    allowed_regions: Optional[List[str]] = None
    allowed_providers: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    required_capabilities: Optional[List[str]] = None
    data_classification: Optional[
        Literal["public", "internal", "sensitive", "restricted"]
    ] = "internal"


class PreflightRequest(BaseModel):
    model: str = Field(description="Requested model or 'auto'")
    messages: List[Message]
    constraints: Optional[PreflightConstraints] = None
    tenant_id: str = Field(
        default="",
        description="Tenant / org identifier. Populated from the X-Supervea-Tenant "
        "header by the endpoint — do not send this in the request body.",
    )
    policy_profile_id: Optional[str] = Field(default=None)


class CandidateRoute(BaseModel):
    route_id: str
    estimated_cost_usd: float
    estimated_latency_ms: int
    status: Literal["eligible", "selected", "rejected"]
    rejection_reason: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    capabilities: Optional[List[str]] = None


class PreflightDecision(BaseModel):
    decision: Literal["route", "fallback_existing_route", "no_route_found"]
    decision_id: str
    route_id: Optional[str] = None
    gateway_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    estimated_latency_ms: Optional[int] = None
    confidence: float = 0.0
    candidates: List[CandidateRoute]
    reason: str
    decision_latency_ms: float


class ObservationIn(BaseModel):
    """A single real-execution outcome, reported AFTER a request was executed.

    Preflight itself never executes requests — it only recommends a
    route_id (see integration_example.py). This is its only source of
    real telemetry: whatever layer actually calls the model is expected
    to report back what really happened (latency, success/failure, actual
    cost) via POST /api/v1/observations, so route_intelligence can be
    built from real outcomes instead of guesses.
    """

    route_id: str
    latency_ms: Optional[int] = Field(default=None, ge=0)
    status_code: Optional[int] = None
    success: bool
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    estimated_cost_usd: Optional[float] = Field(default=None, ge=0)
    actual_cost_usd: Optional[float] = Field(default=None, ge=0)
    metadata: Optional[Dict[str, Any]] = None
