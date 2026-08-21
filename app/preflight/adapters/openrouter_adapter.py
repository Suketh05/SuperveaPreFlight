"""OpenRouter adapter — the primary MVP route source.

Calls OpenRouter's public /models endpoint, maps each model to a
Supervea DiscoveredRoute, and always includes OpenRouter's own
Auto Router (openrouter:auto) as a first-class route so Supervea's
Preflight layer can prove it sits above even a router.
"""

from typing import AsyncIterator

import httpx

from app.preflight.adapters.base import GatewayAdapter
from app.preflight.schemas.internal_models import DiscoveredRoute

# Maps OpenRouter's provider prefix (before the '/') to Supervea's
# canonical provider key. Unrecognized prefixes pass through unchanged.
PROVIDER_PREFIX_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "mistralai": "mistral",
    "meta-llama": "meta",
}

# Fallback pricing for the Auto Router meta-route. In production this
# should be sourced from OpenRouter's published Auto Router pricing or
# a Supervea-configured override — never hard-coded long-term.
AUTO_ROUTER_FALLBACK_PRICING = {
    "input_per_1m": 0.2200,
    "output_per_1m": 0.7500,
    "currency": "USD",
}

# Sanity cap: no genuine model realistically costs more than this per 1M
# tokens. OpenRouter occasionally lists placeholder/non-token pricing
# (e.g. per-request fees on non-chat endpoints) that would otherwise
# overflow the DB's numeric(12,6) column. Anything above this is treated
# as bad data and the route is skipped, not silently truncated.
MAX_REASONABLE_PRICE_PER_1M = 100_000.0


def map_openrouter_model_to_supervea(model_obj: dict) -> DiscoveredRoute:
    """Map one OpenRouter /models entry into a normalized DiscoveredRoute."""
    or_id = model_obj["id"]  # e.g. 'openai/gpt-4o-mini'
    provider_key, _, raw_model = or_id.partition("/")
    provider = PROVIDER_PREFIX_MAP.get(provider_key, provider_key)
    supervea_model = f"{provider}:{raw_model}"

    modality = model_obj.get("architecture", {}).get("modality", "")
    if modality and "text" not in modality.split("->")[-1]:
        raise ValueError(f"Skipping non-text-output model {or_id} (modality={modality})")

    caps = ["chat"]
    if model_obj.get("supports_tools") or "tools" in model_obj.get("supported_parameters", []):
        caps.append("tool_use")
    context_length = model_obj.get("context_length", 0) or 0
    if context_length >= 128_000:
        caps.append("long_context")

    pricing = model_obj.get("pricing", {})
    # OpenRouter's raw API reports price per single token as a string;
    # normalize defensively to price-per-1M as floats.
    input_per_1m = _to_per_million(pricing.get("prompt"))
    output_per_1m = _to_per_million(pricing.get("completion"))

    if input_per_1m > MAX_REASONABLE_PRICE_PER_1M or output_per_1m > MAX_REASONABLE_PRICE_PER_1M:
        raise ValueError(
            f"Implausible price for {or_id}: input={input_per_1m}, output={output_per_1m} "
            f"per 1M tokens. Likely non-token pricing data from OpenRouter; skipping route."
        )

    return DiscoveredRoute(
        external_route_id=or_id,
        provider=provider,
        model=supervea_model,
        model_version=model_obj.get("version"),
        region=model_obj.get("region", "global"),
        capabilities=caps,
        input_price_per_1m=input_per_1m,
        output_price_per_1m=output_per_1m,
        currency="USD",
        advertised_availability=None,
        advertised_latency_p50=None,
        advertised_latency_p95=None,
        data_regions=model_obj.get("data_regions", ["US", "EU"]),
        status="healthy" if model_obj.get("id") else "disabled",
    )


def _to_per_million(price_per_token: str | float | None) -> float:
    """OpenRouter reports price per single token (e.g. '0.0000006'). Convert to per-1M."""
    if price_per_token is None:
        return 0.0
    return round(float(price_per_token) * 1_000_000, 6)


def build_auto_router_route() -> DiscoveredRoute:
    return DiscoveredRoute(
        external_route_id="openrouter/auto",
        provider="openrouter",
        model="openrouter:auto",
        model_version=None,
        region="global",
        capabilities=["chat", "reasoning"],
        input_price_per_1m=AUTO_ROUTER_FALLBACK_PRICING["input_per_1m"],
        output_price_per_1m=AUTO_ROUTER_FALLBACK_PRICING["output_per_1m"],
        currency=AUTO_ROUTER_FALLBACK_PRICING["currency"],
        advertised_availability=None,
        advertised_latency_p50=None,
        advertised_latency_p95=None,
        data_regions=["US", "EU"],
        status="healthy",
    )


class OpenRouterAdapter(GatewayAdapter):
    """
    gateway_config expects:
        {"api_key": "...", "base_url": "https://openrouter.ai/api/v1", "timeout_s": 5.0}
    """

    async def discover_routes(self) -> AsyncIterator[DiscoveredRoute]:
        yield build_auto_router_route()

        response = await self._get("/models")
        skipped = 0
        for item in response.get("data", []):
            try:
                yield map_openrouter_model_to_supervea(item)
            except (KeyError, ValueError, TypeError) as exc:
                # A single malformed/implausible model entry should not
                # break the whole sync — skip it, but say so.
                skipped += 1
                print(f"[openrouter_adapter] Skipping model '{item.get('id', '?')}': {exc}")
                continue
        if skipped:
            print(f"[openrouter_adapter] Skipped {skipped} model(s) with unusable pricing/data.")

    async def get_health(self) -> dict:
        # MVP: coarse health via a cheap, low-timeout call.
        try:
            await self._get("/models", timeout_override=3.0)
            return {"status": "healthy"}
        except httpx.HTTPError:
            return {"status": "unhealthy"}

    async def get_usage(self) -> dict:
        return {}

    async def get_pricing(self) -> dict:
        return {}

    async def get_metadata(self) -> dict:
        return {}

    async def _get(self, path: str, timeout_override: float | None = None) -> dict:
        base_url = self.config.get("base_url", "https://openrouter.ai/api/v1")
        api_key = self.config.get("api_key", "")
        timeout = timeout_override or self.config.get("timeout_s", 5.0)

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json()
