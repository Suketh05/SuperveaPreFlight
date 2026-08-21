"""Hard constraint filtering — the first stage of the lexicographic decision.

Order matters here and mirrors the priority list in the blueprint:
  1. Eliminate invalid/unhealthy/stale routes
  2. Eliminate routes that violate residency, capability, or policy constraints
Anything surviving this stage becomes a *candidate*; cost/latency
constraints and ranking happen afterward in the PreflightEngine.
"""

from app.preflight.core.redis_cache import RedisCache
from app.preflight.schemas.api_models import CandidateRoute, PreflightConstraints
from app.preflight.schemas.internal_models import RequestProfile


class RouteFilter:
    def __init__(self, redis_cache: RedisCache):
        self.redis = redis_cache

    async def hard_filter(
        self,
        profile: RequestProfile,
        candidate_routes: list[dict],
        policy: PreflightConstraints,
    ) -> tuple[list[dict], list[CandidateRoute]]:
        eligible: list[dict] = []
        rejected: list[CandidateRoute] = []

        for route in candidate_routes:
            route_id = str(route["route_id"])
            rejection_reason = self._evaluate(route, policy)

            if rejection_reason:
                rejected.append(
                    CandidateRoute(
                        route_id=route_id,
                        estimated_cost_usd=0.0,
                        estimated_latency_ms=0,
                        status="rejected",
                        rejection_reason=rejection_reason,
                        provider=route.get("provider"),
                        model=route.get("model"),
                        capabilities=route.get("capabilities"),
                    )
                )
                continue

            eligible.append(route)

        return eligible, rejected

    def _evaluate(self, route: dict, policy: PreflightConstraints) -> str | None:
        if route["status"] != "healthy":
            return "route_status_unhealthy"
        if route["data_freshness"] == "stale":
            return "route_data_stale"
        if policy.allowed_regions and not any(
            region in route["data_regions"] for region in policy.allowed_regions
        ):
            return "region_not_allowed"
        if policy.required_capabilities and not set(policy.required_capabilities).issubset(
            set(route["capabilities"])
        ):
            return "missing_capability"
        if policy.allowed_providers and route["provider"] not in policy.allowed_providers:
            return "provider_not_allowed"
        if policy.allowed_models and route["model"] not in policy.allowed_models:
            return "model_not_allowed"
        return None
