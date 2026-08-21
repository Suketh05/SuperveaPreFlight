"""Proves the reported bug is fixed end-to-end at the engine level: a
request with inline `constraints={"allowed_providers": ["made-up-provider"]}`
and no policy_profile_id must fall back, with every candidate rejected as
provider_not_allowed — not be routed as if the constraint were never
evaluated (the exact live curl scenario documented in CLAUDE.md).

Uses fake db/redis collaborators (no live Postgres/Redis), following the
same pattern as test_policy_cache.py and test_decision_cache_key.py.
"""

import pytest

from app.preflight.schemas.api_models import Message, PreflightConstraints, PreflightRequest
from app.preflight.services.estimators import CostEstimator, TokenEstimator
from app.preflight.services.preflight_engine import PreflightEngine
from app.preflight.services.route_filter import RouteFilter


class FakeDB:
    """In-memory stand-in for asyncpg.Connection."""

    def __init__(self, routes: list[dict]):
        self.routes = routes

    async def fetch(self, sql, *args):
        return self.routes

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, *args, **kwargs):
        return None


class FakeRedisCache:
    """In-memory stand-in for RedisCache — always a cache miss, records nothing."""

    async def get_json(self, key):
        return None

    async def set_json(self, key, value, ttl_seconds=None):
        return None

    async def get_json_many(self, keys):
        return {}


def make_route(**overrides) -> dict:
    base = {
        "route_id": "route-1",
        "gateway_id": "gw-1",
        "provider": "openai",
        "model": "openai:gpt-4o-mini",
        "region": "global",
        "capabilities": ["chat", "tool_use"],
        "input_price_per_1m": 0.15,
        "output_price_per_1m": 0.60,
        "currency": "USD",
        "advertised_latency_p95": 700,
        "data_regions": ["US", "EU"],
        "status": "healthy",
        "data_freshness": "fresh",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_inline_allowed_providers_rejects_all_routes_with_real_provider():
    routes = [
        make_route(route_id="route-1", provider="openai"),
        make_route(route_id="route-2", provider="anthropic"),
    ]
    engine = PreflightEngine(
        db=FakeDB(routes),
        redis_cache=FakeRedisCache(),
        token_estimator=TokenEstimator(),
        cost_estimator=CostEstimator(),
        route_filter=RouteFilter(redis_cache=None),
    )
    request = PreflightRequest(
        model="auto",
        messages=[Message(role="user", content="hello")],
        constraints=PreflightConstraints(allowed_providers=["made-up-provider"]),
        tenant_id="demo-tenant",
        policy_profile_id=None,
    )

    decision = await engine.decide(request, mode="observe")

    assert decision.decision == "fallback_existing_route"
    assert len(decision.candidates) == 2
    assert all(c.rejection_reason == "provider_not_allowed" for c in decision.candidates)
    assert all(c.status == "rejected" for c in decision.candidates)
