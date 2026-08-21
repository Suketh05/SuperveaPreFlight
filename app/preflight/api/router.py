"""POST /api/v1/preflight — the standalone Preflight API surface.

This endpoint can be called directly (for testing/observe-mode tooling)
and is also what the existing OpenAI-compatible endpoint wiring calls
into internally — see app/preflight/integration_example.py.
"""

from fastapi import APIRouter, Depends

from app.preflight.api.dependencies import build_preflight_engine, get_tenant_id
from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightDecision, PreflightRequest
from app.preflight.services.fail_open import safe_decide_with_timeout
from app.preflight.services.preflight_engine import PreflightEngine

router = APIRouter(prefix="/api/v1", tags=["preflight"])


@router.post("/preflight", response_model=PreflightDecision)
async def preflight_route(
    body: PreflightRequest,
    tenant_id: str = Depends(get_tenant_id),
    engine: PreflightEngine = Depends(build_preflight_engine),
) -> PreflightDecision:
    body.tenant_id = tenant_id
    return await safe_decide_with_timeout(engine, body, mode=settings.mode)
