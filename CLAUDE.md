# CLAUDE.md — Context for Claude Code

This file exists so you (Claude, working in Claude Code) have full context on
this project without the user needing to re-explain it. Read this fully before
making changes.

## What this project is

**Supervea Preflight MVP** — a thin, deterministic decision layer that sits
*above* existing AI gateways/routers (OpenRouter, Kong, LiteLLM). It looks at
every available model/route across gateways, filters out anything unhealthy,
stale, or policy-violating, estimates cost, and picks the cheapest valid
option — all *before* handing off to Supervea's existing (separate, not in
this repo) optimization layer.

This is an MVP built to test a market hypothesis for a YC-stage startup: can
an independent routing-decision layer above existing AI gateways provide
measurable value, without requiring customers to replace their gateway?

## The two non-negotiable design properties

Any change you make MUST preserve these:

1. **Fail-open, always.** If any part of the decision path errors or times
   out, the system must return a valid `fallback_existing_route` decision,
   never raise an exception up to the HTTP layer. See
   `app/preflight/services/fail_open.py`.
2. **No live gateway calls in the request path.** The `/api/v1/preflight`
   endpoint only ever reads from Postgres/Redis. Adapters (which make real
   HTTP calls to OpenRouter, etc.) only run in the background scheduler
   (`app/preflight/workers/scheduler.py`), never inline with a live request.

## Architecture

Two halves, running at different speeds:

- **Control plane (background, async, slow OK):** `adapters/` call external
  gateway APIs (OpenRouter first, Kong/LiteLLM planned) → `workers/route_normalizer.py`
  writes results into Postgres `routes` table → `workers/scheduler.py` runs
  this on a cadence.
- **Data plane (request-time, must be fast, milliseconds):** `api/router.py`
  receives a request → `services/preflight_engine.py` reads Postgres/Redis
  (never calls gateways live) → filters → cost-estimates → sorts → returns a
  decision. Wrapped end-to-end by `services/fail_open.py`.

## File map

```
app/preflight/
├── schemas/
│   ├── api_models.py       # PreflightRequest, PreflightDecision, etc. (public API shapes)
│   └── internal_models.py  # RequestProfile, DiscoveredRoute (internal only)
├── core/
│   ├── config.py           # ALL env vars read here, nowhere else. settings singleton.
│   └── redis_cache.py      # env-prefixed Redis wrapper
├── adapters/
│   ├── base.py               # GatewayAdapter ABC — the interface every adapter implements
│   └── openrouter_adapter.py  # real OpenRouter /models API call + mapping logic
├── workers/
│   ├── route_normalizer.py  # upserts adapter output into Postgres `routes` table
│   └── scheduler.py         # runs adapter syncs on a cadence (background loop)
├── persistence/
│   └── queries.py           # ALL raw SQL lives here (load_policy, load_candidate_routes, persist_decision)
├── services/
│   ├── estimators.py         # TokenEstimator, CostEstimator — pure math, no I/O
│   ├── route_filter.py       # hard_filter — health/staleness/policy checks
│   ├── preflight_engine.py   # THE core decision logic — decide() method
│   └── fail_open.py          # timeout + exception wrapper around the engine
├── api/
│   ├── router.py             # POST /api/v1/preflight
│   ├── dependencies.py       # FastAPI Depends() wiring (DB/Redis/tenant)
│   └── lifespan.py           # app startup/shutdown — creates DB pool + Redis client
├── integration_example.py    # template for wiring into an existing /v1/chat/completions
└── tests/                    # pytest unit tests (route_filter, estimators, OpenRouter mapping)

app/main.py                   # standalone runnable test harness — NOT for production
migrations/001_preflight_schema.sql  # Postgres schema (6 tables)
scripts/bootstrap_openrouter.py       # one-time: insert OpenRouter gateway row + first sync
```

## Current status — what's built vs. not

**Built and verified working end-to-end** (confirmed via live curl testing
against a real Postgres + Redis + OpenRouter API):
- Full Postgres schema (`gateways`, `routes`, `policy_profiles`,
  `route_observations`, `route_intelligence`, `preflight_decisions`)
- OpenRouter adapter with real `/models` API call and price normalization
  (OpenRouter reports price-per-single-token as a string; converted to
  price-per-1M — see `_to_per_million()` in `openrouter_adapter.py`)
- Route normalizer with partial-failure-safe stale handling (see RESOLVED
  BUG #2 below)
- The decision engine: hard filter → cost estimate → lexicographic sort
  (sort key is `(cost, latency)` — see `preflight_engine.py::decide()`)
- Fail-open wrapper with configurable timeout budget, confirmed sub-2ms
  decision latency in practice once the two bugs below were fixed
- `POST /api/v1/preflight` FastAPI endpoint
- Unit tests for filter logic, estimators, OpenRouter mapping

**Not built yet (known gaps, not bugs):**
- Kong adapter, LiteLLM adapter (same `GatewayAdapter` interface, would be
  a copy of `openrouter_adapter.py`)
- The Redis pricing cache (`<env>:route:pricing:<route_id>`) and policy
  cache (`<env>:policy:profile:<id>`) described in the original blueprint
  are NOT wired up — `CostEstimator` reads price straight from the Postgres
  row on every request, `queries.load_policy()` hits Postgres every
  decision. In practice this has NOT been a latency problem in local
  testing (~1-2ms decisions), but hasn't been load-tested.
- `route_intelligence` roll-up job (table exists, nothing writes to it)
- Health-check polling loop (adapter has `get_health()`, nothing calls it
  on a schedule, so `route:health:<id>` in Redis is never populated —
  `advertised_latency_p95` from the route row is used as a fallback in
  `preflight_engine.py`)

## RESOLVED BUG #1 — tenant_id UUID type mismatch

**Symptom:** every request returned `"decision": "fallback_existing_route"`
with `"reason": "Preflight failed ..."`.

**Root cause:** `tenant_id` columns in the schema (`gateways`,
`policy_profiles`, `route_observations`, `preflight_decisions`) were
declared as Postgres `uuid` type, but the API accepts and forwards *any
string* as a tenant ID via the `X-Supervea-Tenant` header (e.g.
`"demo-tenant"`, not a UUID). Postgres correctly rejected the insert with
`asyncpg.exceptions.DataError: invalid UUID`.

**Fix applied:** `tenant_id` columns changed from `uuid` to `text` in
`migrations/001_preflight_schema.sql`. Internal system-generated IDs
(`gateway_id`, `route_id`, `policy_profile_id`, `decision_id`) correctly
remain `uuid`, since those are always created via `gen_random_uuid()` or
reference other UUID primary keys.

**Also fixed as part of diagnosing this:** `PreflightRequest.tenant_id` in
`schemas/api_models.py` previously had no default value, which caused
Pydantic to reject every request before the endpoint handler even ran
(tenant_id is meant to be populated from the header, not the request body,
so it needs `default=""` to pass validation before being overwritten).

## RESOLVED BUG #2 — one bad route stales the entire gateway

**Symptom:** after bug #1 was fixed, requests succeeded but every single
route came back rejected with `"rejection_reason": "route_data_stale"`,
even though the routes clearly existed with real, correct-looking data.

**Root cause:** in `route_normalizer.py`, `sync_gateway_routes()` wrapped
its entire sync loop in one `try/except`. If *any* single route in the
loop threw an exception — even after dozens of other routes had already
been successfully upserted as `'fresh'` in that same run — the `except`
block ran an unfiltered `UPDATE routes SET data_freshness = 'stale' WHERE
gateway_id = $1`, wiping freshness on every route for that gateway,
including the ones that had just succeeded moments earlier. One bad row
poisoned the entire batch. This was likely triggered by running
`bootstrap_openrouter.py` more than once and hitting a transient failure
partway through a later run.

**Fix applied:** `sync_gateway_routes()` now only calls
`_mark_gateway_routes_stale()` if **zero** routes were synced this run
(total failure). A partial failure leaves the routes that already
succeeded as `'fresh'` and just logs a warning, instead of blanket-staling
everything. Also added `traceback.print_exc()` so any future sync failure
is actually visible in the console instead of silently swallowed.

**To recover already-stale data from before this fix, re-run:**
```bash
python -m scripts.bootstrap_openrouter
```
This re-upserts every route as `'fresh'` again. To unblock immediately
without re-syncing, you can also run:
```sql
UPDATE public.routes SET data_freshness = 'fresh'
WHERE gateway_id = (SELECT gateway_id FROM public.gateways WHERE name = 'OpenRouter');
```
but re-running the bootstrap script is preferred since it also refreshes
prices.

**Lesson worth preserving:** both bugs were caught specifically because
the fail-open wrapper's error was made loud (via `traceback.print_exc()`)
instead of silently swallowed. If you add new failure paths anywhere in
this codebase, always print/log the real exception before falling back —
silent fallbacks are correct for production safety but terrible for
debugging, so keep the diagnostic printing in place even after this bug
is fixed.

## RESOLVED — "all routes rejected as route_data_stale"

**Symptom:** after the `tenant_id` fix above, requests stopped crashing but
still always returned `"decision": "fallback_existing_route"`, this time
with `"reason": "No eligible routes found"` and every one of the 34
candidate routes showing `"rejection_reason": "route_data_stale"`.

**This is actually a good sign, not a new problem:** it means the
end-to-end pipeline (API → filter → audit trail) is working exactly as
designed. The filter correctly excluded every route because the data
genuinely was marked stale in Postgres — nothing was silently wrong.

**Why the routes were stale:** the only code path that sets
`data_freshness = 'stale'` is `RouteNormalizer.sync_gateway_routes()` when
a sync run for that gateway fails with zero routes successfully synced
(see `route_normalizer.py`). This most likely happened because
`bootstrap_openrouter.py` was re-run more than once while debugging the
earlier `tenant_id` bug, and one of those re-runs failed early (network
blip, OpenRouter rate limit, etc.) before it got to upsert anything —
which correctly (by design) marks existing routes for that gateway as
unverified/stale, since we can no longer vouch for their freshness.

**Fix applied:** `route_normalizer.py` now prints the actual exception
(via `traceback.print_exc()`) whenever a sync fails, instead of failing
silently — so if this happens again, the cause will be visible in
whichever terminal ran the sync (or the `uvicorn` terminal, if the
scheduler triggers it in-process). It also already correctly distinguishes
total failure (0 routes synced → mark stale) from partial failure (some
routes synced before an error → leave those as fresh, don't blanket-stale
the whole gateway).

**How to fix your current stale data:** just re-run the sync —

```bash
python -m scripts.bootstrap_openrouter
```

Watch the output closely this time. It should end with `Done. Synced N
routes into public.routes.` with no `[route_normalizer] Sync failed`
message above it. If it does fail, the printed traceback will now show
exactly why (most likely a transient OpenRouter API issue — retry once
more if so).

**Verify it worked** before re-testing the API:
```bash
psql supervea_preflight_v2 -c "SELECT data_freshness, count(*) FROM routes GROUP BY data_freshness;"
```
Should show all (or nearly all) rows as `fresh`, not `stale`.

Then retry:
```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: 11111111-1111-1111-1111-111111111111" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'
```
Expect `"decision": "route"` with a real `model` and `estimated_cost_usd`.

## RESOLVED BUG #3 — numeric field overflow during OpenRouter sync

**Symptom:** `python -m scripts.bootstrap_openrouter` crashed partway
through with `asyncpg.exceptions.NumericValueOutOfRangeError: numeric
field overflow / DETAIL: A field with precision 12, scale 6 must round to
an absolute value less than 10^6`, after successfully syncing 34 routes.

**Root cause:** one OpenRouter model in `/models` reported pricing data
that, after `_to_per_million()` converted it from price-per-token to
price-per-1M-tokens, exceeded the `routes.input_price_per_1m` /
`output_price_per_1m` column's `numeric(12,6)` limit (must be <
1,000,000). Very likely a non-chat model (e.g. an embeddings or image
endpoint) with a non-token pricing convention that doesn't survive the
per-token → per-1M conversion sanely.

**Fix applied:**
1. `adapters/openrouter_adapter.py` — added `MAX_REASONABLE_PRICE_PER_1M`
   sanity cap (100,000). `map_openrouter_model_to_supervea()` now raises
   `ValueError` for implausible prices, caught by the existing per-item
   `try/except` in `discover_routes()` — so one bad model is skipped and
   printed, not fatal. `discover_routes()` also now prints a summary count
   of skipped models at the end of the sync.
2. `workers/route_normalizer.py::sync_gateway_routes()` — added a second,
   inner `try/except` around each individual `_upsert_route()` call, so
   even a DB-level failure on one row (constraint violation, overflow,
   etc.) can never abort the rest of the sync loop. This is defense in
   depth on top of fix #1 — even if a future upstream API sends bad data
   that isn't caught by the price sanity check, the sync as a whole still
   won't crash.

**Because this crashed the earlier sync run, the `routes` table may be
in a partial state** (34 routes fresh, the rest never attempted). Just
re-run the bootstrap script after pulling this fix — it's a safe upsert:
```bash
python -m scripts.bootstrap_openrouter
```
It should now complete with a message like `Skipped 1 model(s) with
unusable pricing/data.` followed by `Done. Synced N routes...` — no
crash, no traceback.

**Lesson worth preserving (same theme as bugs #1 and #2):** validate
upstream data at the earliest possible layer (the adapter/mapping step,
where the error is cheap and specific) AND keep defense-in-depth at the
DB-write layer (the normalizer), so a single malformed record from any
external API — OpenRouter today, Kong/LiteLLM later — can never take down
an entire sync.

## No other known active bugs

If you hit a new issue, document it in this file in the same format:
symptom, root cause once found, fix applied, and any lesson worth
preserving. Keep resolved bugs in this file rather than deleting them —
they're useful history for anyone (human or Claude) working on this repo
later.

**Suggested next step:** add an integration test in `app/preflight/tests/`
that exercises `PreflightEngine.decide()` against a real (or test)
database. The existing tests are pure-function unit tests (`route_filter`,
estimators, OpenRouter mapping) and would NOT have caught either bug above,
since both were about wiring between the API/DB layer and the engine, not
the core decision logic itself.

## How to run this locally

Environment already set up on this machine: Python 3.11 venv (`pfvenv`),
Postgres 15 + Redis via `brew services`, OpenRouter API key in `.env`.

```bash
source pfvenv/bin/activate
set -a; source .env; set +a   # required in every new terminal tab

# Sanity check env actually loaded (should NOT show placeholder values):
echo $SUPERVEA_DATABASE_URL
echo $OPENROUTER_API_KEY

# If routes are missing or stale, re-sync:
python -m scripts.bootstrap_openrouter

# Run the server
uvicorn app.main:app --reload --port 8001
```

In a second terminal (also `source pfvenv/bin/activate; set -a; source .env; set +a`):

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'
```

Expected when working: `"decision": "route"` with a real `model`,
`estimated_cost_usd`, and `gateway_id` populated — not `null`, and
`candidates` showing `"status": "eligible"` or `"selected"`, not a wall of
`"rejection_reason": "route_data_stale"`.

Run tests with:
```bash
pytest -v
```

## Code style / conventions already established in this repo

- All raw SQL lives in `persistence/queries.py` (or, for the sync path,
  `workers/route_normalizer.py`) — never inline SQL elsewhere.
- All env var reads go through `core/config.py`'s `settings` object — never
  call `os.getenv` directly in other files.
- Every adapter implements the `GatewayAdapter` ABC in `adapters/base.py`.
- The engine (`preflight_engine.py`) must never call an adapter directly or
  make any live HTTP call — it only reads from `db`/`redis_cache` passed
  into its constructor. If you're tempted to add a live API call inside
  `decide()`, that belongs in an adapter + background sync instead.
- Any `except Exception` block that's part of a safety/fail-open pattern
  MUST print or log the real exception (see `fail_open.py` and
  `route_normalizer.py` for the pattern) — never swallow silently. Two
  real bugs in this codebase were only found because of this practice.
- Docstrings at the top of each file explain *why*, not just *what* — keep
  that pattern when adding new files.
- Tests use `pytest-asyncio` in `auto` mode (see `pytest.ini`) — no need to
  add `@pytest.mark.asyncio` decorators manually.

## What NOT to change without flagging it to the user first

- The fail-open behavior in `fail_open.py` — this is a deliberate safety
  property, not a bug.
- The `SUPERVEA_PREFLIGHT_ENABLED` default of `false` and
  `SUPERVEA_PREFLIGHT_MODE` default of `observe` in `core/config.py` —
  intentional safe defaults for a staged rollout.
- The unique index `routes_gateway_provider_model_region_uniq` in the
  migration and the `ON CONFLICT` clause in `route_normalizer.py` — these
  two must always be changed together, they're coupled.
- The "only mark stale on total failure" logic in `route_normalizer.py`
  (RESOLVED BUG #2 above) — don't revert to blanket-staling on any
  exception, that was the bug.
