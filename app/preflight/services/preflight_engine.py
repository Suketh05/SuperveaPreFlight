"""The core deterministic decision engine.

Priority order (lexicographic, per the blueprint):
  1. Eliminate invalid routes
  2. Eliminate unhealthy or stale routes
  3. Meet customer latency/cost constraints
  4. Select lowest-cost route
  5. Use latency/reliability as tie-breaker

No LLM calls happen anywhere in this file — the decision must stay
fast, cheap, and reproducible.
"""

import hashlib
import json
import uuid
from time import perf_counter
from typing import Literal, Optional

import asyncpg

from app.preflight.core.redis_cache import RedisCache
from app.preflight.persistence import queries
from app.preflight.schemas.api_models import (
    CandidateRoute,
    Message,
    PreflightConstraints,
    PreflightDecision,
    PreflightRequest,
)
from app.preflight.schemas.internal_models import RequestProfile
from app.preflight.services.estimators import CostEstimator, TokenEstimator
from app.preflight.services.policy_merge import merge_constraints
from app.preflight.services.route_filter import RouteFilter


class PreflightEngine:
    def __init__(
        self,
        db: asyncpg.Connection,
        redis_cache: RedisCache,
        token_estimator: TokenEstimator,
        cost_estimator: CostEstimator,
        route_filter: RouteFilter,
    ):
        self.db = db
        self.redis = redis_cache
        self.token_estimator = token_estimator
        self.cost_estimator = cost_estimator
        self.route_filter = route_filter

    async def decide(
        self, request: PreflightRequest, mode: Literal["observe", "optimize"]
    ) -> PreflightDecision:
        start = perf_counter()
        profile = self._build_profile(request)

        cache_key = self._decision_cache_key(profile, request.constraints)
        cached = await self.redis.get_json(cache_key)
        if cached:
            return PreflightDecision(**cached)

        policy = await queries.load_policy(self.db, profile.policy_profile_id, self.redis)
        # Inline request.constraints never reached RequestProfile (only
        # region/data_classification were extracted into it) and so never
        # reached hard_filter() — a stored policy_profile_id was honored,
        # but ad-hoc constraints on the request itself were silently
        # dropped. Merge them in here, most-restrictive-of-each-field-wins,
        # before the merged policy is ever used for filtering.
        policy = merge_constraints(policy, request.constraints)
        candidate_routes = await queries.load_candidate_routes(self.db, profile)

        eligible, rejected = await self.route_filter.hard_filter(profile, candidate_routes, policy)

        if not eligible:
            return await self._no_route_decision(profile, mode, rejected, start)

        input_tokens, output_tokens = self.token_estimator.estimate_tokens(request.messages)

        candidates: list[CandidateRoute] = rejected.copy()
        enriched: list[tuple[dict, CandidateRoute]] = []
        health_by_route_id: dict[str, dict] = {}

        # Batch the per-route Redis lookups into two MGETs instead of up to
        # 2 round trips PER route — with 20-30+ eligible routes and a 10ms
        # fail-open budget, sequential GETs here is a real latency risk.
        route_ids = [str(r["route_id"]) for r in eligible]
        pricing_map = await self.redis.get_json_many([f"route:pricing:{rid}" for rid in route_ids])
        health_map = await self.redis.get_json_many([f"route:health:{rid}" for rid in route_ids])

        for route in eligible:
            route_id = str(route["route_id"])
            pricing = pricing_map.get(f"route:pricing:{route_id}")
            if pricing is not None:
                priced_route = {
                    **route,
                    "input_price_per_1m": pricing["input_price_per_1m"],
                    "output_price_per_1m": pricing["output_price_per_1m"],
                }
                cost = self.cost_estimator.estimate_cost_usd(priced_route, input_tokens, output_tokens)
            else:
                cost = self.cost_estimator.estimate_cost_usd(route, input_tokens, output_tokens)
            health = health_map.get(f"route:health:{route_id}") or {}
            health_by_route_id[route_id] = health
            latency_ms = health.get("latency_p95_ms") or route.get("advertised_latency_p95") or 1000

            candidate = CandidateRoute(
                route_id=route_id,
                estimated_cost_usd=cost,
                estimated_latency_ms=int(latency_ms),
                status="eligible",
                provider=route["provider"],
                model=route["model"],
                capabilities=route.get("capabilities"),
            )
            candidates.append(candidate)
            enriched.append((route, candidate))

        constrained = [
            (route, cand) for route, cand in enriched if self._within_constraints(cand, policy)
        ]
        if not constrained:
            # No route meets soft constraints (cost/latency caps) -> relax
            # rather than fail outright; still policy/health-compliant.
            constrained = enriched

        constrained.sort(key=lambda rc: (rc[1].estimated_cost_usd, rc[1].estimated_latency_ms))
        selected_route, selected_candidate = constrained[0]

        decision_latency_ms = (perf_counter() - start) * 1000
        decision = PreflightDecision(
            decision="route",
            decision_id=str(uuid.uuid4()),
            route_id=str(selected_route["route_id"]),
            gateway_id=str(selected_route["gateway_id"]),
            provider=selected_route["provider"],
            model=selected_route["model"],
            estimated_cost_usd=selected_candidate.estimated_cost_usd,
            estimated_latency_ms=selected_candidate.estimated_latency_ms,
            confidence=self._estimate_confidence(
                selected_route, health_by_route_id.get(str(selected_route["route_id"]), {})
            ),
            candidates=candidates,
            reason=(
                "Lowest estimated cost among healthy, policy-compliant routes "
                "meeting latency and residency requirements."
            ),
            decision_latency_ms=decision_latency_ms,
        )

        await self._persist(profile, mode, decision)
        await self.redis.set_json(cache_key, decision.model_dump(), ttl_seconds=120)
        return decision

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_profile(self, request: PreflightRequest) -> RequestProfile:
        input_tokens, output_tokens = self.token_estimator.estimate_tokens(request.messages)
        constraints = request.constraints or PreflightConstraints()
        return RequestProfile(
            operation="chat",
            requested_model=request.model,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            capability="general_chat",
            region=(constraints.allowed_regions or ["global"])[0],
            data_classification=constraints.data_classification or "internal",
            tenant_id=request.tenant_id,
            policy_profile_id=request.policy_profile_id,
        )

    def _decision_cache_key(
        self, profile: RequestProfile, constraints: Optional[PreflightConstraints]
    ) -> str:
        # Inline per-request `constraints` (e.g. allowed_providers on an
        # ad-hoc request) must be part of the key — two requests with the
        # same tenant/model/policy_profile_id but different inline
        # constraints are NOT the same decision, and must not share a
        # cached result. sort_keys keeps the hash stable regardless of
        # field insertion order.
        constraints_dict = constraints.model_dump() if constraints is not None else {}
        constraints_json = json.dumps(constraints_dict, sort_keys=True, default=str)
        raw = (
            f"{profile.tenant_id}:{profile.requested_model}:"
            f"{profile.policy_profile_id}:{constraints_json}"
        )
        profile_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"preflight:decision:{profile.tenant_id}:{profile_hash}"

    def _within_constraints(self, candidate: CandidateRoute, policy: PreflightConstraints) -> bool:
        if policy.max_cost_usd is not None and candidate.estimated_cost_usd > policy.max_cost_usd:
            return False
        if policy.max_latency_ms is not None and candidate.estimated_latency_ms > policy.max_latency_ms:
            return False
        return True

    def _estimate_confidence(self, route: dict, health: dict) -> float:
        # Prefer real confidence from route_intelligence (built from actual
        # observed outcomes — see workers/intelligence_rollup.py). Only
        # fall back to the flat heuristic when no observations exist yet.
        if health.get("confidence") is not None:
            return float(health["confidence"])
        return 0.8 if route.get("data_freshness") == "fresh" else 0.4

    async def _no_route_decision(
        self,
        profile: RequestProfile,
        mode: str,
        rejected: list[CandidateRoute],
        start: float,
    ) -> PreflightDecision:
        decision_latency_ms = (perf_counter() - start) * 1000
        decision = PreflightDecision(
            decision="fallback_existing_route",
            decision_id=str(uuid.uuid4()),
            candidates=rejected,
            reason="No eligible routes found; falling back to existing route.",
            decision_latency_ms=decision_latency_ms,
        )
        await self._persist(profile, mode, decision)
        return decision

    async def _persist(self, profile: RequestProfile, mode: str, decision: PreflightDecision) -> None:
        request_hash = hashlib.sha256(
            f"{profile.tenant_id}:{profile.requested_model}".encode()
        ).hexdigest()
        await queries.persist_decision(
            self.db,
            tenant_id=profile.tenant_id,
            request_hash=request_hash,
            mode=mode,
            selected_route_id=decision.route_id,
            decision_type=decision.decision,
            candidates=decision.candidates,
            reason=decision.reason,
            decision_latency_ms=decision.decision_latency_ms,
        )
