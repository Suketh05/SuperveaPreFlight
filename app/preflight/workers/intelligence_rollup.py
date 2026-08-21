"""Rolls up raw `route_observations` telemetry into `route_intelligence`
and a Redis health cache — this is what finally makes the engine's
confidence score real instead of a hardcoded 0.8/0.4 guess.

This is a ROLLUP of observations that already happened, NOT a live
per-route health prober that goes out and pings routes on a timer. That
was a deliberate choice, not an oversight:

  1. OpenRouter alone exposes 30+ chat routes. Actively pinging all of
     them every 15-30s is wasted load on someone else's API for a signal
     that real production traffic already gives us for free, the moment
     something actually calls POST /api/v1/observations.
  2. A synthetic "is it up" ping doesn't capture what we actually care
     about — real chat-completion latency and success rate under real
     prompt sizes and real load. A route can happily answer a 1-token
     ping and still time out on a 4k-token completion; rollup from real
     observations doesn't have that blind spot.
  3. The repo's "no live gateway calls in the request path" rule (see
     CLAUDE.md) is about the DATA PLANE — the /api/v1/preflight request
     path must never make a live call. This module is control plane,
     same as route_normalizer.py's adapter syncs, so it isn't off the
     table on that rule alone. We're choosing not to build a live prober
     anyway, on cost/quality grounds above — this is a deliberate design
     choice, not a hard constraint we're routing around.

Because of this, `route_intelligence` and the `route:health:<id>` Redis
keys stay empty (and the engine keeps falling back to its flat
0.8/0.4 confidence heuristic) until something actually reports real
outcomes via POST /api/v1/observations. Nothing in this repo does that
yet — that's the existing (separate) optimization layer's job, since
it's the thing that actually executes the selected route and knows what
really happened.
"""

import asyncio
import logging

import asyncpg

from app.preflight.core.redis_cache import RedisCache
from app.preflight.persistence import queries

logger = logging.getLogger("supervea.preflight.intelligence_rollup")

# Observation count at which we consider a route's rolling window fully
# trustworthy. Below this, confidence is scaled down proportionally —
# a route with 2 successful observations shouldn't score as confidently
# as one with 500.
FULL_CONFIDENCE_OBSERVATIONS = 50


def compute_route_health(agg_row: dict) -> dict:
    """Pure function: one row from fetch_observation_aggregates -> a health dict.

    No I/O here on purpose — keeps this testable without a database.
    """
    success_rate_5m = agg_row.get("success_rate_5m")
    success_rate_1h = agg_row.get("success_rate_1h")
    success_rate_24h = agg_row.get("success_rate_24h")

    if success_rate_5m is not None:
        availability = success_rate_5m
    elif success_rate_1h is not None:
        availability = success_rate_1h
    elif success_rate_24h is not None:
        availability = success_rate_24h
    else:
        availability = 1.0

    observation_count_24h = agg_row.get("observation_count_24h") or 0
    observation_confidence = min(1.0, observation_count_24h / FULL_CONFIDENCE_OBSERVATIONS)
    confidence = round(observation_confidence * (0.5 + 0.5 * availability), 4)

    error_rate_5m = agg_row.get("error_rate_5m")
    health_score = round(availability * (1.0 - min(1.0, error_rate_5m or 0.0)), 4)

    latency_p50_ms = agg_row.get("latency_p50_ms")
    latency_p95_ms = agg_row.get("latency_p95_ms")

    observed_cost_per_request = agg_row.get("observed_cost_per_request")
    observed_cost_per_1k_tokens = (
        round(observed_cost_per_request * 1000, 6) if observed_cost_per_request is not None else None
    )

    return {
        "availability": availability,
        "observation_confidence": observation_confidence,
        "confidence": confidence,
        "health_score": health_score,
        "latency_p50_ms": int(latency_p50_ms) if latency_p50_ms is not None else None,
        "latency_p95_ms": int(latency_p95_ms) if latency_p95_ms is not None else None,
        "success_rate_5m": success_rate_5m,
        "success_rate_1h": success_rate_1h,
        "success_rate_24h": success_rate_24h,
        "error_rate_5m": error_rate_5m,
        "observed_cost_per_1k_tokens": observed_cost_per_1k_tokens,
        "observation_count_24h": observation_count_24h,
        "staleness": "fresh",
    }


def _health_cache_key(route_id) -> str:
    return f"route:health:{route_id}"


async def run_intelligence_rollup_once(db_pool: asyncpg.Pool, redis_cache: RedisCache) -> int:
    """Runs one rollup pass. Returns the number of routes updated."""
    updated = 0
    async with db_pool.acquire() as conn:
        agg_rows = await queries.fetch_observation_aggregates(conn)

        for agg_row in agg_rows:
            route_id = agg_row["route_id"]
            try:
                health = compute_route_health(agg_row)
                await queries.upsert_route_intelligence(conn, str(route_id), health)
                await redis_cache.set_json(_health_cache_key(route_id), health, ttl_seconds=None)
                updated += 1
            except Exception:
                # One bad route must never abort the rest of the rollup —
                # same defense-in-depth principle as route_normalizer.py.
                logger.exception("Intelligence rollup failed for route_id=%s", route_id)

    return updated


async def run_intelligence_rollup_loop(
    db_pool: asyncpg.Pool, redis_cache: RedisCache, interval_seconds: int = 20
) -> None:
    """Runs forever. Cancel the task to stop (e.g. on app shutdown)."""
    while True:
        try:
            updated = await run_intelligence_rollup_once(db_pool, redis_cache)
            logger.info("Intelligence rollup updated %d routes", updated)
        except Exception:
            logger.exception("Intelligence rollup pass failed")
        await asyncio.sleep(interval_seconds)
