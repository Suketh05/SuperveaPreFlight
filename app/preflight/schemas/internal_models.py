"""Internal normalized models used only within the Preflight engine.

RequestProfile is the compact, normalized shape every incoming
PreflightRequest is converted into before the engine touches it.
"""

from typing import Literal, Optional

from pydantic import BaseModel


class RequestProfile(BaseModel):
    operation: Literal["chat"]  # MVP: only chat is supported
    requested_model: str  # 'auto', 'gpt-4o-mini', 'openrouter:auto', etc.
    estimated_input_tokens: int
    estimated_output_tokens: int
    capability: Literal[
        "general_chat",
        "code_assistant",
        "tool_use",
        "long_context",
    ]
    region: str  # desired execution region, e.g. 'EU'
    data_classification: Literal["public", "internal", "sensitive", "restricted"]
    tenant_id: str
    policy_profile_id: Optional[str] = None


class DiscoveredRoute(BaseModel):
    """What a GatewayAdapter reports for a single route it found upstream."""

    external_route_id: str
    provider: str
    model: str
    model_version: Optional[str] = None
    region: str
    capabilities: list[str]
    input_price_per_1m: float
    output_price_per_1m: float
    currency: str = "USD"
    advertised_availability: Optional[float] = None
    advertised_latency_p50: Optional[int] = None
    advertised_latency_p95: Optional[int] = None
    data_regions: list[str]
    status: str  # 'healthy', 'degraded', 'unhealthy', 'disabled'
