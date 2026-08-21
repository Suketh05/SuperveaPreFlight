"""
EXAMPLE ONLY — shows how to wire Preflight into your EXISTING
OpenAI-compatible /v1/chat/completions endpoint. Do not import this
file directly; copy the pattern into your existing endpoint code.

The key properties preserved here:
  - SUPERVEA_PREFLIGHT_ENABLED=false -> behaviour is 100% unchanged.
  - Preflight failures/timeouts fall back to today's execution path.
  - Preflight never proxies the request itself — it only recommends
    a route_id, which your EXISTING optimizer/executor then uses.

Also shown here: after your executor actually runs the selected route,
report the real outcome back via POST /api/v1/observations. Preflight
never executes requests itself, so this is its only source of real
telemetry — without it, route_intelligence and the engine's confidence
score never move past a flat guess (see workers/intelligence_rollup.py
and CLAUDE.md's Session 2 notes for why).
"""

import logging

from app.preflight.api.dependencies import build_preflight_engine
from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightRequest
from app.preflight.services.fail_open import safe_decide_with_timeout

logger = logging.getLogger("supervea.preflight.integration_example")


async def chat_completions_with_preflight(request, tenant_id: str, existing_app):
    """
    `request`      = your existing ChatCompletionRequest object
    `existing_app`  = wherever execute_without_preflight /
                       execute_with_selected_route / _map_request_to_constraints /
                       _lookup_policy_profile_id already live in your codebase
    """
    model = request.model or "auto"

    if not settings.enabled:
        return await existing_app.execute_without_preflight(request, tenant_id)

    preflight_request = PreflightRequest(
        model=model,
        messages=request.messages,
        constraints=existing_app.map_request_to_constraints(request),
        tenant_id=tenant_id,
        policy_profile_id=existing_app.lookup_policy_profile_id(tenant_id),
    )

    engine = await build_preflight_engine()  # in practice: resolved via Depends
    decision = await safe_decide_with_timeout(engine, preflight_request, mode=settings.mode)

    if decision.decision == "fallback_existing_route":
        return await existing_app.execute_without_preflight(request, tenant_id)

    response = await existing_app.execute_with_selected_route(
        request=request,
        tenant_id=tenant_id,
        route_id=decision.route_id,
    )

    # Report the real outcome back so route_intelligence reflects reality,
    # not a guess. This must never affect the response the customer gets —
    # swallow and log any failure here.
    #
    # try:
    #     import httpx
    #     from app.preflight.schemas.api_models import ObservationIn
    #
    #     await httpx.AsyncClient().post(
    #         "http://localhost:8001/api/v1/observations",
    #         headers={"X-Supervea-Tenant": tenant_id},
    #         json=ObservationIn(
    #             route_id=decision.route_id,
    #             latency_ms=response.latency_ms,       # from your executor's timing
    #             status_code=response.status_code,
    #             success=response.status_code < 400,
    #             actual_cost_usd=response.actual_cost_usd,  # if your executor tracks it
    #         ).model_dump(),
    #     )
    # except Exception:
    #     logger.exception("Failed to report observation for route_id=%s", decision.route_id)

    return response
