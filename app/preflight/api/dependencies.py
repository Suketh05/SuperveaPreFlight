"""FastAPI dependency wiring for the Preflight module.

Keeps connection/lifecycle concerns out of the route handlers. In a real
deployment, db_pool and redis_client should be created once at app
startup (see app/preflight/api/router.py for the lifespan hook) and
reused across requests rather than opened per-request.
"""

from typing import AsyncGenerator

import asyncpg
from fastapi import Depends, Header, Request
from redis.asyncio import Redis

from app.preflight.core.config import settings
from app.preflight.core.redis_cache import RedisCache
from app.preflight.services.estimators import CostEstimator, TokenEstimator
from app.preflight.services.preflight_engine import PreflightEngine
from app.preflight.services.route_filter import RouteFilter


async def get_tenant_id(x_tenant_id: str = Header(..., alias="X-Supervea-Tenant")) -> str:
    return x_tenant_id


async def get_db_connection(request: Request) -> AsyncGenerator[asyncpg.Connection, None]:
    pool: asyncpg.Pool = request.app.state.preflight_db_pool
    async with pool.acquire() as conn:
        yield conn


def get_redis_cache(request: Request) -> RedisCache:
    client: Redis = request.app.state.preflight_redis_client
    return RedisCache(client, env=settings.environment)


async def build_preflight_engine(
    db: asyncpg.Connection = Depends(get_db_connection),
    redis_cache: RedisCache = Depends(get_redis_cache),
) -> PreflightEngine:
    return PreflightEngine(
        db=db,
        redis_cache=redis_cache,
        token_estimator=TokenEstimator(),
        cost_estimator=CostEstimator(),
        route_filter=RouteFilter(redis_cache),
    )
