"""Raw DB access functions used by the Preflight engine.

Kept separate from the engine so the engine's decision logic stays
testable without a live database (mock these functions in unit tests).
"""

import json

import asyncpg

from app.preflight.schemas.api_models import PreflightConstraints
from app.preflight.schemas.internal_models import RequestProfile

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


async def load_policy(db: asyncpg.Connection, policy_profile_id: str | None) -> PreflightConstraints:
    if not policy_profile_id:
        return PreflightConstraints()

    row = await db.fetchrow(LOAD_POLICY_SQL, policy_profile_id)
    if not row:
        return PreflightConstraints()

    return PreflightConstraints(
        allowed_providers=row["allowed_providers"],
        allowed_models=row["allowed_models"],
        allowed_regions=row["allowed_regions"],
        max_cost_usd=row["max_cost_usd"],
        max_latency_ms=row["max_latency_ms"],
        required_capabilities=row["required_capabilities"],
        data_classification=row["data_classification"],
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
    import uuid

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
