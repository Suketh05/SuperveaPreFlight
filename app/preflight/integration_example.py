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

from app.preflight.api.dependencies import run_preflight_decision
from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightRequest
from app.preflight.services.fail_open import safe_decide_with_timeout

logger = logging.getLogger("supervea.preflight.integration_example")


async def chat_completions_with_preflight(request, tenant_id: str, existing_app, fastapi_request):
    """
    `request`         = your existing ChatCompletionRequest object
    `existing_app`     = wherever execute_without_preflight /
                          execute_with_selected_route / _map_request_to_constraints /
                          _lookup_policy_profile_id already live in your codebase
    `fastapi_request`  = the `Request` your endpoint already receives —
                          needed so run_preflight_decision can reach
                          `app.state.preflight_db_pool` / `.preflight_redis_client`
                          itself, INSIDE the fail-open try/except below,
                          instead of a Depends() acquiring the connection
                          before this function ever runs (see fail_open.py's
                          docstring — that ordering is what let a Postgres
                          outage surface as a raw 500 instead of a clean
                          fallback_existing_route decision).
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

    decision = await safe_decide_with_timeout(
        run_preflight_decision(fastapi_request, preflight_request, settings.mode)
    )

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
