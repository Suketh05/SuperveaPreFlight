"""Preflight module configuration.

Reads from environment variables. Keep this the single source of truth
for feature flags and connection settings so the rest of the module
never reaches into os.environ directly.
"""

import os
from typing import Literal


class PreflightSettings:
    # Feature flag: master on/off switch. When False, the module is a no-op
    # and existing routing behaviour is completely unchanged.
    enabled: bool = os.getenv("SUPERVEA_PREFLIGHT_ENABLED", "false").lower() == "true"

    # 'observe'  -> decision is computed and logged, but NOT used for execution
    # 'optimize' -> decision controls which route is actually used
    mode: Literal["observe", "optimize"] = os.getenv("SUPERVEA_PREFLIGHT_MODE", "observe")  # type: ignore

    environment: str = os.getenv("SUPERVEA_ENV", "prod")

    database_url: str = os.getenv("SUPERVEA_DATABASE_URL", "")
    redis_url: str = os.getenv("SUPERVEA_REDIS_URL", "redis://localhost:6379/0")

    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    # Fail-open budget: if a decision can't be made within this window, fall back.
    decision_timeout_ms: int = int(os.getenv("SUPERVEA_PREFLIGHT_TIMEOUT_MS", "10"))

    # Decision cache TTL (seconds)
    decision_cache_ttl_seconds: int = int(os.getenv("SUPERVEA_PREFLIGHT_DECISION_CACHE_TTL", "120"))

    # Policy cache TTL (seconds)
    policy_cache_ttl_seconds: int = int(os.getenv("SUPERVEA_PREFLIGHT_POLICY_CACHE_TTL", "300"))

    # Pricing cache TTL (seconds)
    pricing_cache_ttl_seconds: int = int(os.getenv("SUPERVEA_PREFLIGHT_PRICING_CACHE_TTL", "3600"))


settings = PreflightSettings()
