"""Raw DB access functions used by the Preflight engine.

Kept separate from the engine so the engine's decision logic stays
testable without a live database (mock these functions in unit tests).
"""

import json
import uuid
from datetime import datetime, timezone

import asyncpg

from app.preflight.core.config import settings
from app.preflight.core.redis_cache import RedisCache
from app.preflight.schemas.api_models import ObservationIn, PreflightConstraints
from app.preflight.schemas.internal_models import RequestProfile

LOAD_ACTIVE_GATEWAYS_SQL = """
SELECT gateway_id, tenant_id, name, type, endpoint, environment, region,
       status, adapter_version
FROM public.gateways
WHERE status != 'disabled';
"""

LOAD_POLICY_SQL = """
SELECT allowed_providers, allowed_models, allowed_regions,
       max_cost_usd, max_latency_ms, required_capabilities, data_classification
FROM public.policy_profiles
WHERE policy_profile_id = $1;
"""

# MVP candidate-route query: healthy-or-stale routes matching requested
# model (or all routes, if the client asked for 'auto'). Region filtering
# happens in the hard filter, not here, so we can log rejections properly.
LOAD_CANDIDATE_ROUTES_SQL = """
SELECT route_id, gateway_id, provider, model, region, capabilities,
       input_price_per_1m, output_price_per_1m, currency,
       advertised_latency_p95, data_regions, status, data_freshness
FROM public.routes
WHERE ($1 = 'auto' OR model = $1)
ORDER BY route_id;
"""

INSERT_DECISION_SQL = """
INSERT INTO public.preflight_decisions (
    decision_id, tenant_id, request_hash, mode, selected_route_id,
    decision_type, candidates, reason, decision_latency_ms
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
"""


async def load_active_gateways(db: asyncpg.Connection) -> list[dict]:
    rows = await db.fetch(LOAD_ACTIVE_GATEWAYS_SQL)
    return [dict(row) for row in rows]


async def load_policy(
    db: asyncpg.Connection,
    policy_profile_id: str | None,
    redis_cache: RedisCache | None = None,
) -> PreflightConstraints:
    if not policy_profile_id:
        return PreflightConstraints()

    cache_key = f"policy:profile:{policy_profile_id}"
    if redis_cache is not None:
        cached = await redis_cache.get_json(cache_key)
        if cached is not None:
            return PreflightConstraints(**cached)

    row = await db.fetchrow(LOAD_POLICY_SQL, policy_profile_id)
    if not row:
        return PreflightConstraints()

    constraints = PreflightConstraints(
        allowed_providers=row["allowed_providers"],
        allowed_models=row["allowed_models"],
        allowed_regions=row["allowed_regions"],
        max_cost_usd=row["max_cost_usd"],
        max_latency_ms=row["max_latency_ms"],
        required_capabilities=row["required_capabilities"],
        data_classification=row["data_classification"],
    )

    if redis_cache is not None:
        await redis_cache.set_json(
            cache_key, constraints.model_dump(), ttl_seconds=settings.policy_cache_ttl_seconds
        )

    return constraints


RECORD_OBSERVATION_SQL = """
INSERT INTO public.route_observations (
    route_id, tenant_id, timestamp, latency_ms, status_code, success,
    input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd, metadata
) VALUES ($1, $2, now(), $3, $4, $5, $6, $7, $8, $9, $10);
"""

# One row per route_id with any observation in the last 24h. Windowed
# rates (5m/1h/24h) are computed with FILTER rather than separate
# queries so a single scan of the last 24h of observations produces the
# whole rollup for that route.
AGGREGATE_OBSERVATIONS_SQL = """
SELECT
    route_id,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE latency_ms IS NOT NULL) AS latency_p50_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE latency_ms IS NOT NULL) AS latency_p95_ms,
    avg(CASE WHEN success THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE timestamp >= now() - interval '5 minutes') AS success_rate_5m,
    avg(CASE WHEN success THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE timestamp >= now() - interval '1 hour') AS success_rate_1h,
    avg(CASE WHEN success THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE timestamp >= now() - interval '24 hours') AS success_rate_24h,
    avg(CASE WHEN NOT success THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE timestamp >= now() - interval '5 minutes') AS error_rate_5m,
    avg(estimated_cost_usd)
        FILTER (WHERE timestamp >= now() - interval '24 hours') AS observed_cost_per_request,
    count(*)
        FILTER (WHERE timestamp >= now() - interval '24 hours') AS observation_count_24h
FROM public.route_observations
WHERE timestamp >= now() - interval '24 hours'
GROUP BY route_id;
"""

UPSERT_ROUTE_INTELLIGENCE_SQL = """
INSERT INTO public.route_intelligence (
    route_id, observed_cost_per_1k_tokens, latency_p50_ms, latency_p95_ms,
    success_rate_5m, success_rate_1h, success_rate_24h, error_rate_5m,
    availability, confidence, last_updated
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (route_id) DO UPDATE SET
    observed_cost_per_1k_tokens = EXCLUDED.observed_cost_per_1k_tokens,
    latency_p50_ms               = EXCLUDED.latency_p50_ms,
    latency_p95_ms                = EXCLUDED.latency_p95_ms,
    success_rate_5m               = EXCLUDED.success_rate_5m,
    success_rate_1h               = EXCLUDED.success_rate_1h,
    success_rate_24h              = EXCLUDED.success_rate_24h,
    error_rate_5m                 = EXCLUDED.error_rate_5m,
    availability                  = EXCLUDED.availability,
    confidence                    = EXCLUDED.confidence,
    last_updated                  = EXCLUDED.last_updated;
"""


async def record_observation(
    db: asyncpg.Connection, route_id: str, tenant_id: str | None, obs: ObservationIn
) -> None:
    await db.execute(
        RECORD_OBSERVATION_SQL,
        uuid.UUID(route_id),
        tenant_id,
        obs.latency_ms,
        obs.status_code,
        obs.success,
        obs.input_tokens,
        obs.output_tokens,
        obs.estimated_cost_usd,
        obs.actual_cost_usd,
        json.dumps(obs.metadata) if obs.metadata is not None else None,
    )


async def fetch_observation_aggregates(db: asyncpg.Connection) -> list[dict]:
    rows = await db.fetch(AGGREGATE_OBSERVATIONS_SQL)
    return [dict(row) for row in rows]


async def upsert_route_intelligence(db: asyncpg.Connection, route_id: str, health: dict) -> None:
    await db.execute(
        UPSERT_ROUTE_INTELLIGENCE_SQL,
        uuid.UUID(route_id),
        health.get("observed_cost_per_1k_tokens"),
        health.get("latency_p50_ms"),
        health.get("latency_p95_ms"),
        health.get("success_rate_5m"),
        health.get("success_rate_1h"),
        health.get("success_rate_24h"),
        health.get("error_rate_5m"),
        health.get("availability"),
        health.get("confidence"),
        datetime.now(timezone.utc),
    )


async def load_candidate_routes(db: asyncpg.Connection, profile: RequestProfile) -> list[dict]:
    rows = await db.fetch(LOAD_CANDIDATE_ROUTES_SQL, profile.requested_model)
    return [dict(row) for row in rows]


async def persist_decision(
    db: asyncpg.Connection,
    tenant_id: str,
    request_hash: str,
    mode: str,
    selected_route_id: str | None,
    decision_type: str,
    candidates: list,
    reason: str,
    decision_latency_ms: float,
) -> None:
    await db.execute(
        INSERT_DECISION_SQL,
        uuid.uuid4(),
        tenant_id,
        request_hash,
        mode,
        selected_route_id,
        decision_type,
        json.dumps([c.model_dump() for c in candidates]),
        reason,
        decision_latency_ms,
    )
