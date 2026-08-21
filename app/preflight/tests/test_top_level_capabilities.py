"""Proves the top-level PreflightDecision.capabilities field (added as a
readability improvement so the winning route's capabilities don't require
cross-referencing route_id against the `candidates` list) actually agrees
with the matching candidate's capabilities, not just that the field exists.

Uses fake db/redis collaborators (no live Postgres/Redis), following the
same pattern as test_inline_constraints_integration.py.
"""

import pytest

from app.preflight.schemas.api_models import Message, PreflightRequest
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
async def test_top_level_capabilities_match_selected_candidate():
    routes = [
        make_route(
            route_id="route-1",
            provider="openai",
            capabilities=["chat", "tool_use"],
            input_price_per_1m=0.15,
            output_price_per_1m=0.60,
        ),
        make_route(
            route_id="route-2",
            provider="anthropic",
            capabilities=["chat", "long_context"],
            input_price_per_1m=5.0,
            output_price_per_1m=15.0,
        ),
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
        tenant_id="demo-tenant",
    )

    decision = await engine.decide(request, mode="observe")

    assert decision.decision == "route"
    assert decision.capabilities is not None

    matching_candidates = [c for c in decision.candidates if c.route_id == decision.route_id]
    assert len(matching_candidates) == 1
    assert decision.capabilities == matching_candidates[0].capabilities
    # The cheaper route (route-1) should win, so this also pins the field
    # to real, non-trivial data rather than two coincidentally-equal lists.
    assert decision.capabilities == ["chat", "tool_use"]


@pytest.mark.asyncio
async def test_no_route_decision_has_explicit_none_capabilities():
    routes = [make_route(route_id="route-1", provider="openai")]
    engine = PreflightEngine(
        db=FakeDB(routes),
        redis_cache=FakeRedisCache(),
        token_estimator=TokenEstimator(),
        cost_estimator=CostEstimator(),
        route_filter=RouteFilter(redis_cache=None),
    )
    from app.preflight.schemas.api_models import PreflightConstraints

    request = PreflightRequest(
        model="auto",
        messages=[Message(role="user", content="hello")],
        constraints=PreflightConstraints(allowed_providers=["made-up-provider"]),
        tenant_id="demo-tenant",
    )

    decision = await engine.decide(request, mode="observe")

    assert decision.decision == "fallback_existing_route"
    assert decision.capabilities is None
