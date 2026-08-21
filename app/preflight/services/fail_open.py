"""Fail-open wrapper: the single most important safety property of Preflight.

If the decision can't be produced within the configured budget, or throws
for any reason, we ALWAYS fall back to existing routing. Customer traffic
must never be blocked or broken by this module.

Callers pass in the *coroutine* that produces the decision (not an
already-built PreflightEngine) so that everything needed to reach a
decision — including acquiring a DB connection — runs inside this
function's try/except. A DB connection acquired earlier, e.g. via a
FastAPI `Depends()`, resolves BEFORE the route handler body runs, which
means a failure there (Postgres unreachable, etc.) would never reach this
wrapper and would surface as a raw, unhandled 500 instead of a clean
fallback_existing_route decision. See router.py's `run_preflight_decision`
for the coroutine this is meant to wrap.
"""

import asyncio
import logging
import traceback
import uuid
from typing import Awaitable

from app.preflight.core.config import settings
from app.preflight.schemas.api_models import PreflightDecision

logger = logging.getLogger("supervea.preflight")


async def safe_decide_with_timeout(
    decision: Awaitable[PreflightDecision],
    timeout_ms: int | None = None,
) -> PreflightDecision:
    budget_ms = timeout_ms or settings.decision_timeout_ms
    try:
        return await asyncio.wait_for(
            decision,
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
