"""One-time bootstrap: insert the OpenRouter gateway row and run the
first catalogue sync so there are routes in the DB before you hit the
API. Run this once after applying the migration.

    python -m scripts.bootstrap_openrouter
"""

import asyncio

import asyncpg
from redis.asyncio import Redis

from app.preflight.core.config import settings
from app.preflight.core.redis_cache import RedisCache
from app.preflight.workers.scheduler import run_catalogue_sync_once

FIND_GATEWAY_SQL = "SELECT gateway_id, name, type FROM public.gateways WHERE name = $1;"

INSERT_GATEWAY_SQL = """
INSERT INTO public.gateways (name, type, endpoint, environment, region, status, adapter_version)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING gateway_id, name, type;
"""


async def main() -> None:
    if not settings.database_url:
        raise SystemExit("SUPERVEA_DATABASE_URL is not set. Check your .env file.")

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    redis_cache = RedisCache(redis_client, env=settings.environment)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(FIND_GATEWAY_SQL, "OpenRouter")
        if existing:
            gateway_row = dict(existing)
            print(f"Gateway already exists: {gateway_row}")
        else:
            row = await conn.fetchrow(
                INSERT_GATEWAY_SQL,
                "OpenRouter",
                "openrouter",
                settings.openrouter_base_url,
                settings.environment,
                "global",
                "healthy",
                "v0.1.0",
            )
            gateway_row = dict(row)
            print(f"Inserted gateway: {gateway_row}")

    print("Running first catalogue sync (this calls the real OpenRouter API)...")
    synced = await run_catalogue_sync_once(pool, gateway_row, redis_cache=redis_cache)
    print(f"Done. Synced {synced} routes into public.routes.")

    await pool.close()
    await redis_client.close()


if __name__ == "__main__":
    asyncio.run(main())
