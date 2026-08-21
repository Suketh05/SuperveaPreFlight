"""Startup/shutdown wiring for the Preflight module's connections.

Import `preflight_lifespan` into your existing FastAPI app's lifespan
context (or call these two functions from your existing startup/shutdown
event handlers if you're not on the lifespan API yet).
"""

from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from redis.asyncio import Redis

from app.preflight.core.config import settings


async def init_preflight_connections(app: FastAPI) -> None:
    if not settings.enabled:
        return
    app.state.preflight_db_pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    app.state.preflight_redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


async def close_preflight_connections(app: FastAPI) -> None:
    if not settings.enabled:
        return
    pool = getattr(app.state, "preflight_db_pool", None)
    if pool:
        await pool.close()
    client = getattr(app.state, "preflight_redis_client", None)
    if client:
        await client.close()


@asynccontextmanager
async def preflight_lifespan(app: FastAPI):
    await init_preflight_connections(app)
    yield
    await close_preflight_connections(app)
