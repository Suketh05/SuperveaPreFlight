"""POST /api/v1/preflight — the standalone Preflight API surface.

This endpoint can be called directly (for testing/observe-mode tooling)
and is also what the existing OpenAI-compatible endpoint wiring calls
into internally — see app/preflight/integration_example.py.
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.preflight.api.dependencies import build_preflight_engine, get_db_connection, get_tenant_id
from app.preflight.core.config import settings
from app.preflight.persistence import queries
from app.preflight.schemas.api_models import ObservationIn, PreflightDecision, PreflightRequest
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


@router.post("/observations", status_code=202)
async def record_observation(
    body: ObservationIn,
    tenant_id: str = Depends(get_tenant_id),
    db: asyncpg.Connection = Depends(get_db_connection),
) -> dict:
    try:
        await queries.record_observation(db, body.route_id, tenant_id, body)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid route_id: {exc}")
    return {"status": "recorded"}
