"""Fail-open wrapper: the single most important safety property of Preflight.

If the engine can't produce a decision within the configured budget, or
throws for any reason, we ALWAYS fall back to existing routing. Customer
traffic must never be blocked or broken by this module.
"""

import asyncio
import logging
import traceback
import uuid

from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightDecision, PreflightRequest
from app.preflight.services.preflight_engine import PreflightEngine

logger = logging.getLogger("supervea.preflight")


async def safe_decide_with_timeout(
    engine: PreflightEngine,
    request: PreflightRequest,
    mode: str,
    timeout_ms: int | None = None,
) -> PreflightDecision:
    budget_ms = timeout_ms or settings.decision_timeout_ms
    try:
        return await asyncio.wait_for(
            engine.decide(request, mode=mode),  # type: ignore[arg-type]
            timeout=budget_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        print(
            f"[preflight] TIMED OUT after {budget_ms}ms — the engine.decide() call "
            f"did not finish in time. This means something inside decide() is slow "
            f"or hanging (DB query, Redis call, etc.), not necessarily an error."
        )
        logger.exception("Preflight decision timed out; falling back to existing route.")
        return PreflightDecision(
            decision="fallback_existing_route",
            decision_id=str(uuid.uuid4()),
            candidates=[],
            reason="Preflight timed out; using existing configured route.",
            decision_latency_ms=float(budget_ms),
        )
    except Exception as exc:
        # Print unconditionally to stderr so this is visible even if the
        # logging module isn't configured with a handler in this process.
        print("[preflight] EXCEPTION in engine.decide():")
        traceback.print_exc()
        logger.exception("Preflight decision failed; falling back to existing route.")
        return PreflightDecision(
            decision="fallback_existing_route",
            decision_id=str(uuid.uuid4()),
            candidates=[],
            reason=f"Preflight failed ({type(exc).__name__}); using existing configured route.",
            decision_latency_ms=float(budget_ms),
        )
