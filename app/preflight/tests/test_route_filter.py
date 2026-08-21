import pytest

from app.preflight.schemas.api_models import PreflightConstraints
from app.preflight.services.route_filter import RouteFilter


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
async def test_healthy_route_passes_with_no_policy():
    route_filter = RouteFilter(redis_cache=None)
    eligible, rejected = await route_filter.hard_filter(
        profile=None, candidate_routes=[make_route()], policy=PreflightConstraints()
    )
    assert len(eligible) == 1
    assert len(rejected) == 0


@pytest.mark.asyncio
async def test_unhealthy_route_is_rejected():
    route_filter = RouteFilter(redis_cache=None)
    eligible, rejected = await route_filter.hard_filter(
        profile=None,
        candidate_routes=[make_route(status="unhealthy")],
        policy=PreflightConstraints(),
    )
    assert len(eligible) == 0
    assert rejected[0].rejection_reason == "route_status_unhealthy"


@pytest.mark.asyncio
async def test_stale_route_is_rejected():
    route_filter = RouteFilter(redis_cache=None)
    eligible, rejected = await route_filter.hard_filter(
        profile=None,
        candidate_routes=[make_route(data_freshness="stale")],
        policy=PreflightConstraints(),
    )
    assert len(eligible) == 0
    assert rejected[0].rejection_reason == "route_data_stale"


@pytest.mark.asyncio
async def test_region_not_allowed_is_rejected():
    route_filter = RouteFilter(redis_cache=None)
    policy = PreflightConstraints(allowed_regions=["APAC"])
    eligible, rejected = await route_filter.hard_filter(
        profile=None, candidate_routes=[make_route()], policy=policy
    )
    assert len(eligible) == 0
    assert rejected[0].rejection_reason == "region_not_allowed"


@pytest.mark.asyncio
async def test_missing_capability_is_rejected():
    route_filter = RouteFilter(redis_cache=None)
    policy = PreflightConstraints(required_capabilities=["long_context"])
    eligible, rejected = await route_filter.hard_filter(
        profile=None, candidate_routes=[make_route()], policy=policy
    )
    assert len(eligible) == 0
    assert rejected[0].rejection_reason == "missing_capability"


@pytest.mark.asyncio
async def test_provider_not_allowed_is_rejected():
    route_filter = RouteFilter(redis_cache=None)
    policy = PreflightConstraints(allowed_providers=["anthropic"])
    eligible, rejected = await route_filter.hard_filter(
        profile=None, candidate_routes=[make_route()], policy=policy
    )
    assert len(eligible) == 0
    assert rejected[0].rejection_reason == "provider_not_allowed"
