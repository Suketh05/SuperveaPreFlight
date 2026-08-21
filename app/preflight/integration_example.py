"""
EXAMPLE ONLY — shows how to wire Preflight into your EXISTING
OpenAI-compatible /v1/chat/completions endpoint. Do not import this
file directly; copy the pattern into your existing endpoint code.

The key properties preserved here:
  - SUPERVEA_PREFLIGHT_ENABLED=false -> behaviour is 100% unchanged.
  - Preflight failures/timeouts fall back to today's execution path.
  - Preflight never proxies the request itself — it only recommends
    a route_id, which your EXISTING optimizer/executor then uses.
"""

from app.preflight.api.dependencies import build_preflight_engine
from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightRequest
from app.preflight.services.fail_open import safe_decide_with_timeout


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

    return await existing_app.execute_with_selected_route(
        request=request,
        tenant_id=tenant_id,
        route_id=decision.route_id,
    )
