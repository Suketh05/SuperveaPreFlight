# Supervea Preflight MVP (OpenRouter-first)

A thin, deterministic decision layer that sits above existing AI gateways
and routers (OpenRouter, Kong, LiteLLM), compares available execution
routes, and picks the best one before handing off to Supervea's existing
optimization layer.

This is a standalone, importable module (`app/preflight/`) designed to be
dropped into your existing FastAPI app with minimal surface area.

## What's implemented

- **Data model** — Postgres schema for `gateways`, `routes`,
  `policy_profiles`, `route_observations`, `route_intelligence`,
  `preflight_decisions` (`migrations/001_preflight_schema.sql`)
- **Redis cache wrapper** — env-prefixed keys, JSON get/set
  (`app/preflight/core/redis_cache.py`)
- **OpenRouter adapter** — real `/models` API call, price normalization,
  OpenRouter → Supervea route mapping, Auto Router as a first-class route
  (`app/preflight/adapters/openrouter_adapter.py`)
- **Route normalizer** — upserts adapter output into `routes`, marks
  stale on adapter failure (`app/preflight/workers/route_normalizer.py`)
- **Background scheduler** — runs adapter syncs on a cadence
  (`app/preflight/workers/scheduler.py`)
- **Decision engine** — deterministic, lexicographic route selection:
  health/staleness → policy/region/capability → cost/latency constraints
  → cheapest → latency tiebreak (`app/preflight/services/preflight_engine.py`)
- **Fail-open wrapper** — hard timeout budget, any failure falls back to
  existing routing (`app/preflight/services/fail_open.py`)
- **FastAPI endpoint** — `POST /api/v1/preflight`
  (`app/preflight/api/router.py`)
- **Integration example** — how to wire this into your existing
  `/v1/chat/completions` endpoint without changing its behaviour when
  disabled (`app/preflight/integration_example.py`)
- **Unit tests** — route filter logic, cost/token estimators, OpenRouter
  mapping (`app/preflight/tests/`)

## Not yet implemented (next steps)

- Kong adapter, LiteLLM adapter (same `GatewayAdapter` interface —
  copy `openrouter_adapter.py` as a template)
- `route_intelligence` roll-up job (currently only raw observations are
  modeled; the confidence score is a flat heuristic)
- RLS policies at the Supabase level for multi-tenant isolation
- Real health-check polling loop (5-30s cadence) — the adapter has
  `get_health()`, but nothing calls it on a schedule yet
- Load-testing the sub-10ms decision budget under real DB/Redis latency

## Getting started (local demo, ~10 minutes)

**Prerequisites:** Python 3.11+, Docker Desktop, a free OpenRouter API
key from https://openrouter.ai/keys

```bash
# 0. Unzip and enter the project
cd supervea-preflight
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. Start Postgres and Redis (throwaway local containers)
docker run -d --name preflight-postgres -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:15
docker run -d --name preflight-redis -p 6379:6379 redis:7

# 2. Configure environment
cp .env.example .env
# Edit .env and set:
#   SUPERVEA_PREFLIGHT_ENABLED=true
#   SUPERVEA_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
#   SUPERVEA_REDIS_URL=redis://localhost:6379/0
#   OPENROUTER_API_KEY=<your real key>
set -a; source .env; set +a   # load .env into this shell

# 3. Apply the schema
docker exec -i preflight-postgres psql -U postgres -d postgres < migrations/001_preflight_schema.sql

# 4. Seed the OpenRouter gateway + run the first route sync (real API call)
python -m scripts.bootstrap_openrouter

# 5. Run the app
uvicorn app.main:app --reload --port 8001
```

In another terminal, try it:

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'
```

You should get back a `PreflightDecision` JSON with a selected route,
its estimated cost/latency, and the full candidate list.

**Cleanup when done:**
```bash
docker stop preflight-postgres preflight-redis
docker rm preflight-postgres preflight-redis
```

## Running tests

```bash
pytest
```

(Tests were written and syntax-checked in this environment but not
executed live, since this build environment has no network access to
install dependencies. Install `requirements.txt` and run `pytest` in
your own environment before deploying.)

## Integrating into your existing app

Do NOT run `app/main.py` in production — it's a standalone test harness.
Instead, in your existing FastAPI app:

```python
from app.preflight.api.router import router as preflight_router
from app.preflight.api.lifespan import init_preflight_connections, close_preflight_connections

app.include_router(preflight_router)

@app.on_event("startup")
async def startup():
    await init_preflight_connections(app)
    # ... your existing startup code

@app.on_event("shutdown")
async def shutdown():
    await close_preflight_connections(app)
    # ... your existing shutdown code
```

Then follow the pattern in `app/preflight/integration_example.py` to wire
the decision into your existing `/v1/chat/completions` handler. Keep
`SUPERVEA_PREFLIGHT_ENABLED=false` until you've run observe-mode long
enough to trust the decisions, per the phased rollout plan.

## Safety properties (do not remove when extending this)

- **Fail-open always.** Any exception or timeout in the decision path
  must fall back to existing routing, never raise into customer traffic.
- **No live gateway calls in the data plane.** The `/api/v1/preflight`
  endpoint only ever reads from Postgres/Redis — adapters only run in
  the background scheduler.
- **No raw prompts persisted.** `preflight_decisions.request_hash` is a
  hash, never the raw message content.
