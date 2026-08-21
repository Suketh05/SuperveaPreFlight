"""Background adapter scheduler (Control Plane).

Runs each configured gateway adapter on its own cadence and writes
results into the Route Registry via RouteNormalizer. This is meant to
run as a long-lived background task (Celery beat, or an asyncio loop
started at FastAPI startup) — never inside the request/response path.

Cadence guidance from the blueprint:
  - catalogue/config sync: 5-60 min
  - pricing sync:          1-6 hours
  - health checks:         5-30 sec
"""

import asyncio
import logging

import asyncpg

from app.preflight.adapters.base import GatewayAdapter
from app.preflight.adapters.litellm_adapter import LiteLLMAdapter
from app.preflight.adapters.openrouter_adapter import OpenRouterAdapter
from app.preflight.core.config import settings
from app.preflight.core.redis_cache import RedisCache
from app.preflight.workers.route_normalizer import RouteNormalizer

logger = logging.getLogger("supervea.preflight.scheduler")

# gateway_row lookups are simplified here; in production, load these
# rows from the `gateways` table instead of hard-coding.
ADAPTER_REGISTRY: dict[str, type[GatewayAdapter]] = {
    "openrouter": OpenRouterAdapter,
    "litellm": LiteLLMAdapter,
}


async def run_catalogue_sync_once(
    db_pool: asyncpg.Pool, gateway_row: dict, redis_cache: RedisCache | None = None
) -> int:
    adapter_cls = ADAPTER_REGISTRY.get(gateway_row["type"])
    if adapter_cls is None:
        logger.warning("No adapter registered for gateway type=%s", gateway_row["type"])
        return 0

    adapter = adapter_cls(
        {
            "api_key": settings.openrouter_api_key,
            "base_url": settings.openrouter_base_url,
            "timeout_s": 5.0,
        }
    )

    async with db_pool.acquire() as conn:
        normalizer = RouteNormalizer(conn, redis_cache=redis_cache)
        synced = await normalizer.sync_gateway_routes(gateway_row, adapter)
        logger.info("Synced %d routes for gateway=%s", synced, gateway_row["name"])
        return synced


async def run_catalogue_sync_loop(
    db_pool: asyncpg.Pool,
    gateway_rows: list[dict],
    interval_seconds: int = 900,
    redis_cache: RedisCache | None = None,
) -> None:
    """Runs forever. Cancel the task to stop (e.g. on app shutdown)."""
    while True:
        for gateway_row in gateway_rows:
            try:
                await run_catalogue_sync_once(db_pool, gateway_row, redis_cache=redis_cache)
            except Exception:
                logger.exception("Catalogue sync failed for gateway=%s", gateway_row.get("name"))
        await asyncio.sleep(interval_seconds)
