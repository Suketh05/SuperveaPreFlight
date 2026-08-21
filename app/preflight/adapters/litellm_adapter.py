"""LiteLLM adapter — a second, independent real gateway route source.

Unlike OpenRouter's adapter, this one doesn't call a live LiteLLM proxy
deployment at all: LiteLLM publishes its entire model/pricing catalog as
a static, public, no-auth JSON file in its own GitHub repo —

    https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json

— which is the same data every LiteLLM proxy instance ships with
bundled. Fetching that file is a real HTTP call against real, current,
third-party data (not mocked, not hand-written), so once this adapter's
routes sit alongside OpenRouter's in the `routes` table, cross-gateway
cost/route comparison is genuinely proving something — without requiring
anyone to stand up an actual LiteLLM proxy server just to get real data
from a second gateway.

The catalog mixes every mode LiteLLM supports (chat, completion,
embedding, image_generation, audio_transcription, audio_speech,
image_edit, realtime, rerank, video_generation, search, ocr,
moderation, ...) into one flat file — well over a dozen non-chat modes
were confirmed present in the live catalog while building this adapter.
Without filtering to `mode in ("chat", "completion")`, this adapter
would reproduce the exact same class of bug already documented in this
project's history for OpenRouter (a non-text-output model slipping
through and becoming an "eligible" route it should never have been) —
see CLAUDE.md's LiteLLM adapter session notes for specifics on what the
live catalog actually contains.
"""

from typing import AsyncIterator

import httpx

from app.preflight.adapters.base import GatewayAdapter
from app.preflight.schemas.internal_models import DiscoveredRoute

DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

# LiteLLM's catalog file includes a literal "sample_spec" entry that is a
# documentation template (field descriptions as string values, e.g.
# `"mode": "one of: chat, embedding, completion, ..."`), not a real model.
# It would already fail the mode filter below (its "mode" value isn't
# literally "chat" or "completion"), but skipping it by name first gives
# a clearer diagnostic than an incidental modality-filter rejection.
SAMPLE_SPEC_KEY = "sample_spec"

# Sanity cap: no genuine model realistically costs more than this per 1M
# tokens. Confirmed against the real live catalog while building this
# adapter: a handful of entries (all under the "wandb/" provider prefix)
# report input/output costs that convert to well over 100,000 per 1M
# tokens — clearly bad upstream pricing data, not real prices. Anything
# above this is treated as bad data and the route is skipped, not
# silently truncated. Same cap value as OpenRouter's adapter.
MAX_REASONABLE_PRICE_PER_1M = 100_000.0


def _to_per_million(price_per_token: float | None) -> float:
    """LiteLLM reports cost per single token (e.g. 1.5e-07). Convert to per-1M."""
    if price_per_token is None:
        return 0.0
    return round(float(price_per_token) * 1_000_000, 6)


def map_litellm_model_to_supervea(model_key: str, model_obj: dict) -> DiscoveredRoute:
    """Map one entry of LiteLLM's model_prices_and_context_window.json into a
    normalized DiscoveredRoute.
    """
    if model_key == SAMPLE_SPEC_KEY:
        raise ValueError("Skipping sample_spec — a documentation template, not a real model")

    mode = model_obj.get("mode")
    if mode not in ("chat", "completion"):
        raise ValueError(f"Skipping non-chat model {model_key} (mode={mode})")

    input_cost = model_obj.get("input_cost_per_token")
    output_cost = model_obj.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        raise ValueError(f"Skipping {model_key}: missing input/output cost data")

    provider = model_obj.get("litellm_provider", "unknown")
    supervea_model = f"{provider}:{model_key}"

    input_per_1m = _to_per_million(input_cost)
    output_per_1m = _to_per_million(output_cost)

    if input_per_1m > MAX_REASONABLE_PRICE_PER_1M or output_per_1m > MAX_REASONABLE_PRICE_PER_1M:
        raise ValueError(
            f"Implausible price for {model_key}: input={input_per_1m}, output={output_per_1m} "
            f"per 1M tokens. Likely bad pricing data from LiteLLM's catalog; skipping route."
        )

    caps = ["chat"]
    if model_obj.get("supports_function_calling"):
        caps.append("tool_use")
    if (model_obj.get("max_input_tokens") or 0) >= 128_000:
        caps.append("long_context")

    return DiscoveredRoute(
        external_route_id=f"litellm/{model_key}",
        provider=provider,
        model=supervea_model,
        model_version=None,
        region="global",
        capabilities=caps,
        input_price_per_1m=input_per_1m,
        output_price_per_1m=output_per_1m,
        currency="USD",
        advertised_availability=None,
        advertised_latency_p50=None,
        advertised_latency_p95=None,
        data_regions=["US", "EU"],
        status="healthy",
    )


class LiteLLMAdapter(GatewayAdapter):
    """
    gateway_config expects:
        {"catalog_url": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
         "timeout_s": 5.0}
    `catalog_url` defaults to LiteLLM's public GitHub-hosted catalog if not provided.
    """

    async def discover_routes(self) -> AsyncIterator[DiscoveredRoute]:
        catalog = await self._get_catalog()
        skipped = 0
        for model_key, model_obj in catalog.items():
            try:
                yield map_litellm_model_to_supervea(model_key, model_obj)
            except (KeyError, ValueError, TypeError) as exc:
                # A single malformed/non-chat/implausible model entry should
                # not break the whole sync — skip it, but say so.
                skipped += 1
                print(f"[litellm_adapter] Skipping model '{model_key}': {exc}")
                continue
        if skipped:
            print(f"[litellm_adapter] Skipped {skipped} model(s) with unusable modality/pricing/data.")

    async def get_health(self) -> dict:
        # MVP: coarse health via a cheap, low-timeout call.
        try:
            await self._get_catalog(timeout_override=3.0)
            return {"status": "healthy"}
        except httpx.HTTPError:
            return {"status": "unhealthy"}

    async def get_usage(self) -> dict:
        return {}

    async def get_pricing(self) -> dict:
        return {}

    async def get_metadata(self) -> dict:
        return {}

    async def _get_catalog(self, timeout_override: float | None = None) -> dict:
        catalog_url = self.config.get("catalog_url", DEFAULT_CATALOG_URL)
        timeout = timeout_override or self.config.get("timeout_s", 5.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(catalog_url)
            resp.raise_for_status()
            return resp.json()
