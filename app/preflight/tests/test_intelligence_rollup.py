from app.preflight.workers.intelligence_rollup import (
    FULL_CONFIDENCE_OBSERVATIONS,
    compute_route_health,
)


def make_agg_row(**overrides) -> dict:
    base = {
        "route_id": "route-1",
        "latency_p50_ms": 400,
        "latency_p95_ms": 900,
        "success_rate_5m": 1.0,
        "success_rate_1h": 1.0,
        "success_rate_24h": 1.0,
        "error_rate_5m": 0.0,
        "observed_cost_per_request": 0.002,
        "observation_count_24h": FULL_CONFIDENCE_OBSERVATIONS,
    }
    base.update(overrides)
    return base


def test_perfectly_healthy_high_volume_route_scores_near_one():
    health = compute_route_health(make_agg_row())
    assert health["availability"] == 1.0
    assert health["confidence"] == 1.0
    assert health["health_score"] == 1.0


def test_low_observation_count_caps_confidence_even_at_100pct_success():
    health = compute_route_health(make_agg_row(observation_count_24h=5))
    # observation_confidence = 5/50 = 0.1 -> confidence = 0.1 * (0.5 + 0.5*1.0) = 0.1
    assert health["confidence"] == 0.1
    # availability/health_score are unaffected by volume, only confidence is
    assert health["availability"] == 1.0
    assert health["health_score"] == 1.0


def test_high_error_rate_drags_down_health_score():
    health = compute_route_health(
        make_agg_row(success_rate_5m=0.5, error_rate_5m=0.5)
    )
    assert health["availability"] == 0.5
    assert health["health_score"] == 0.25


def test_availability_falls_back_through_windows():
    # No 5m data, but 1h data exists -> use 1h
    health = compute_route_health(
        make_agg_row(success_rate_5m=None, success_rate_1h=0.9, success_rate_24h=0.7)
    )
    assert health["availability"] == 0.9

    # No 5m or 1h data, only 24h -> use 24h
    health = compute_route_health(
        make_agg_row(success_rate_5m=None, success_rate_1h=None, success_rate_24h=0.7)
    )
    assert health["availability"] == 0.7

    # No data at all -> default to 1.0 (don't punish a route we've never observed)
    health = compute_route_health(
        make_agg_row(success_rate_5m=None, success_rate_1h=None, success_rate_24h=None)
    )
    assert health["availability"] == 1.0


def test_zero_observations_yields_zero_confidence():
    health = compute_route_health(make_agg_row(observation_count_24h=0))
    assert health["confidence"] == 0.0


def test_missing_latency_data_returns_none_not_a_crash():
    health = compute_route_health(
        make_agg_row(latency_p50_ms=None, latency_p95_ms=None)
    )
    assert health["latency_p50_ms"] is None
    assert health["latency_p95_ms"] is None
