"""Startup/shutdown wiring for the Preflight module's connections.

Import `preflight_lifespan` into your existing FastAPI app's lifespan
context (or call these two functions from your existing startup/shutdown
event handlers if you're not on the lifespan API yet).
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from redis.asyncio import Redis

from app.preflight.core.config import settings
from app.preflight.core.redis_cache import RedisCache
from app.preflight.persistence import queries
from app.preflight.workers.intelligence_rollup import run_intelligence_rollup_loop
from app.preflight.workers.scheduler import run_catalogue_sync_loop

logger = logging.getLogger("supervea.preflight.lifespan")


async def init_preflight_connections(app: FastAPI) -> None:
    if not settings.enabled:
        return
    app.state.preflight_db_pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    app.state.preflight_redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.preflight_background_tasks = []

    # Starting the background sync/rollup loops is best-effort: if the
    # migration hasn't been applied yet, or no gateways have been
    # bootstrapped, the data plane should still come up fine and just
    # return no_route_found until gateways exist — it must never take
    # down app startup.
    try:
        redis_cache = RedisCache(app.state.preflight_redis_client, env=settings.environment)
        async with app.state.preflight_db_pool.acquire() as conn:
            gateway_rows = await queries.load_active_gateways(conn)

        catalogue_task = asyncio.create_task(
            run_catalogue_sync_loop(
                app.state.preflight_db_pool, gateway_rows, redis_cache=redis_cache
            )
        )
        rollup_task = asyncio.create_task(
            run_intelligence_rollup_loop(app.state.preflight_db_pool, redis_cache)
        )
        app.state.preflight_background_tasks = [catalogue_task, rollup_task]
    except Exception:
        logger.warning(
            "Failed to start Preflight background jobs (catalogue sync / "
            "intelligence rollup) — continuing with an empty route registry. "
            "This is expected if the migration hasn't been applied yet or no "
            "gateways have been bootstrapped (see scripts/bootstrap_openrouter.py).",
            exc_info=True,
        )
        app.state.preflight_background_tasks = []


async def close_preflight_connections(app: FastAPI) -> None:
    if not settings.enabled:
        return

    tasks = getattr(app.state, "preflight_background_tasks", [])
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Preflight background task raised on shutdown")

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
