"""Normalizes adapter output into the Route Registry (Postgres).

Annexure mitigation: if an adapter fails mid-sync, its existing routes
are marked stale (not deleted, not marked unhealthy) — the Preflight
engine treats stale routes as ineligible, so a dead adapter degrades
route freshness rather than corrupting decisions with bad data.

IMPORTANT: staleness is only applied when a sync produces ZERO routes.
A partial failure (some routes synced successfully, then an error) does
NOT blanket-stale the routes that already succeeded in this same run —
only a total failure marks the gateway's existing routes stale. This
avoids one bad row poisoning an otherwise-successful sync.
"""

import traceback
from datetime import datetime, timezone

import asyncpg

from app.preflight.adapters.base import GatewayAdapter
from app.preflight.schemas.internal_models import DiscoveredRoute

UPSERT_ROUTE_SQL = """
INSERT INTO public.routes (
    gateway_id, provider, model, model_version, route_kind,
    region, capabilities,
    input_price_per_1m, output_price_per_1m, currency,
    advertised_availability, advertised_latency_p50, advertised_latency_p95,
    data_regions, status, data_freshness, last_successful_sync
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7,
    $8, $9, $10,
    $11, $12, $13,
    $14, $15, 'fresh', $16
)
ON CONFLICT (gateway_id, provider, model, region)
DO UPDATE SET
    capabilities             = EXCLUDED.capabilities,
    input_price_per_1m       = EXCLUDED.input_price_per_1m,
    output_price_per_1m      = EXCLUDED.output_price_per_1m,
    currency                 = EXCLUDED.currency,
    advertised_availability  = EXCLUDED.advertised_availability,
    advertised_latency_p50   = EXCLUDED.advertised_latency_p50,
    advertised_latency_p95   = EXCLUDED.advertised_latency_p95,
    data_regions              = EXCLUDED.data_regions,
    status                    = EXCLUDED.status,
    data_freshness            = 'fresh',
    last_successful_sync      = EXCLUDED.last_successful_sync,
    last_updated               = now();
"""

MARK_STALE_SQL = """
UPDATE public.routes
SET data_freshness = 'stale'
WHERE gateway_id = $1;
"""


class RouteNormalizer:
    def __init__(self, db: asyncpg.Connection):
        self.db = db

    async def sync_gateway_routes(self, gateway_row: dict, adapter: GatewayAdapter) -> int:
        """Sync one gateway's routes. Returns the number of routes upserted."""
        synced = 0
        skipped = 0
        try:
            async for discovered in adapter.discover_routes():
                try:
                    await self._upsert_route(gateway_row, discovered)
                    synced += 1
                except Exception as exc:
                    # A single bad row (bad data, constraint violation, etc.)
                    # must never abort the rest of the sync.
                    skipped += 1
                    print(
                        f"[route_normalizer] Skipping route '{discovered.model}' "
                        f"for gateway={gateway_row['name']}: {exc}"
                    )
        except Exception:
            print(
                f"[route_normalizer] Sync failed for gateway={gateway_row.get('name')} "
                f"after successfully syncing {synced} routes. Exception:"
            )
            traceback.print_exc()
            if synced == 0:
                # Nothing succeeded this run -> the gateway's existing data is
                # now unverified, so degrade it to stale.
                await self._mark_gateway_routes_stale(gateway_row["gateway_id"])
            else:
                # Partial success: the `synced` routes above were already
                # upserted as 'fresh' in this same run. Do NOT blanket-stale
                # the whole gateway, that would incorrectly wipe freshness on
                # routes that just succeeded moments ago.
                print(
                    f"[route_normalizer] {synced} routes synced successfully "
                    f"before the failure — leaving those as 'fresh', not "
                    f"marking the whole gateway stale."
                )
        return synced

    async def _upsert_route(self, gateway_row: dict, discovered: DiscoveredRoute) -> None:
        route_kind = (
            "router"
            if discovered.model == "openrouter:auto" and discovered.provider == "openrouter"
            else "model"
        )
        now = datetime.now(timezone.utc)

        await self.db.execute(
            UPSERT_ROUTE_SQL,
            gateway_row["gateway_id"],
            discovered.provider,
            discovered.model,
            discovered.model_version,
            route_kind,
            discovered.region,
            discovered.capabilities,
            discovered.input_price_per_1m,
            discovered.output_price_per_1m,
            discovered.currency,
            discovered.advertised_availability,
            discovered.advertised_latency_p50,
            discovered.advertised_latency_p95,
            discovered.data_regions,
            discovered.status,
            now,
        )

    async def _mark_gateway_routes_stale(self, gateway_id: str) -> None:
        await self.db.execute(MARK_STALE_SQL, gateway_id)
