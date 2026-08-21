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
│   ├── openrouter_adapter.py  # real OpenRouter /models API call + mapping logic
│   └── litellm_adapter.py     # real LiteLLM public model/pricing catalog + mapping logic
├── workers/
│   ├── route_normalizer.py  # upserts adapter output into Postgres `routes` table
│   ├── scheduler.py         # runs adapter syncs on a cadence (background loop)
│   └── intelligence_rollup.py  # rolls up route_observations into route_intelligence + Redis health
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
scripts/bootstrap_litellm.py          # one-time: insert LiteLLM gateway row + first sync
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
- Kong adapter (same `GatewayAdapter` interface — LiteLLM's adapter,
  added in Session 3, is the template to copy)
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

## Session 2

Wired up the two Redis caches (`route:pricing:<route_id>` and
`policy:profile:<id>`) that were described in the original blueprint but,
per the "Not built yet" section above, were never actually connected —
every decision was reading policy and price straight from Postgres on
every request. This was fine at MVP request volume in local testing, but
defeats the purpose of having a data plane that's supposed to avoid
hitting Postgres on the hot path for data that changes rarely (policy
profiles) or on a slow adapter cadence (pricing).

**Config (`core/config.py`):** added `policy_cache_ttl_seconds` (env
`SUPERVEA_PREFLIGHT_POLICY_CACHE_TTL`, default 300s) and
`pricing_cache_ttl_seconds` (env `SUPERVEA_PREFLIGHT_PRICING_CACHE_TTL`,
default 3600s), following the same `os.getenv` pattern as
`decision_cache_ttl_seconds`. Two separate TTLs because the two caches
have very different natural staleness windows — policy profiles are
edited rarely and correctness-sensitive, pricing changes on the adapter
sync cadence (1-6 hours per the scheduler docstring).

**Policy cache (`persistence/queries.py::load_policy`):** now takes an
optional `redis_cache` param. On a cache hit it returns
`PreflightConstraints(**cached)` without ever touching Postgres. On a
miss (or when `redis_cache` is `None`, e.g. tests that don't wire Redis),
it falls through to the existing `db.fetchrow` logic unchanged, then
populates the cache before returning. The `if not policy_profile_id`
short-circuit stays first and skips both Redis and Postgres — no policy
profile means no lookup needed either way. `PreflightEngine.decide()` now
passes `self.redis` into this call.

**Pricing cache (`workers/route_normalizer.py` +
`services/preflight_engine.py`):** the pricing side is populated as a
side effect of the *existing* sync path rather than a separate cache-fill
step, since pricing only ever changes when an adapter sync writes it.
`UPSERT_ROUTE_SQL` now ends in `RETURNING route_id` and `_upsert_route`
uses `fetchval` instead of `execute` so it has the route's UUID (needed
whether the row was an insert or an on-conflict update) to key the cache
entry. `RouteNormalizer` takes an optional `redis_cache` — when present,
every successful upsert also writes `route:pricing:<route_id>` with the
price, currency, and a fresh timestamp. On the read side,
`PreflightEngine.decide()` checks `route:pricing:<route_id>` before
costing each eligible route; on a hit it builds a `priced_route` dict
(the Postgres row with just the two price fields overridden) and costs
that instead. **Postgres stays canonical** — a cache miss (cold cache,
TTL expired, or bootstrap run before this change) just falls back to the
price already loaded on the route row from `load_candidate_routes`, so
this is a pure latency optimization, not a new source of truth.

**Threading `redis_cache` through the scheduler and bootstrap script:**
`run_catalogue_sync_once` and `run_catalogue_sync_loop` in
`workers/scheduler.py` both gained an optional `redis_cache: RedisCache |
None = None` param that's passed straight into `RouteNormalizer(conn,
redis_cache=redis_cache)`. It defaults to `None` specifically so any
existing caller that doesn't know about this yet keeps working exactly
as before (cache simply doesn't get populated on that path, no error).
`scripts/bootstrap_openrouter.py` now builds a `Redis`/`RedisCache` pair
using the same pattern as `init_preflight_connections` in
`api/lifespan.py`, and passes it into `run_catalogue_sync_once` so a
fresh bootstrap run also warms the pricing cache — previously it only
ever populated Postgres, so the pricing cache stayed empty until the
first scheduled sync ran (or forever, in a dev setup that never starts
the scheduler).

**Tests:** `app/preflight/tests/test_policy_cache.py` uses in-memory fake
`db`/`redis_cache` objects with call counters (no real Postgres/Redis) to
prove `load_policy` is genuinely cache-through: a second call with the
same `policy_profile_id` does not call `fetchrow` again, a cache miss
populates the cache with the exact dict `PreflightConstraints(**cached)`
can reconstruct from, `redis_cache=None` falls through to the DB every
call (no caching, no crash), and the empty-`policy_profile_id`
short-circuit never touches either the DB or Redis.

**What this does NOT change:** the pricing cache is intentionally
best-effort and read-only from the engine's perspective — nothing in
`preflight_engine.py` ever writes to `route:pricing:*`, only
`route_normalizer.py` does, during a sync. If you're debugging a decision
that looks like it's using stale pricing, check whether the pricing
cache's TTL (1 hour default) has outlived the actual Postgres price (e.g.
after a manual `UPDATE routes ...`), since a manual DB edit does not
invalidate the cache — only a fresh adapter sync does.

### Built an observation -> intelligence rollup, made confidence real

`route_intelligence` existed as a table since the initial migration but
nothing ever wrote to it, and `PreflightEngine._estimate_confidence()`
was a flat `0.8` (fresh) / `0.4` (stale) guess. Fixed by building the
actual pipeline the table was designed for: real execution outcomes come
in via a new `POST /api/v1/observations` endpoint, get rolled up on a
cadence into `route_intelligence` + a Redis health cache, and the engine
reads that cache to compute a real confidence score.

**New file `workers/intelligence_rollup.py`** — this is a ROLLUP of
`route_observations`, deliberately **not** a live per-route health
prober that goes and pings routes on a timer. Three reasons, worth
re-reading if you're ever tempted to "upgrade" this to active probing:
1. OpenRouter alone exposes 30+ chat routes — live-pinging all of them
   every 15-30s is wasted load on someone else's API for a signal real
   traffic already gives us for free the moment something reports an
   observation.
2. A synthetic "is it up" ping doesn't capture what actually matters —
   real chat-completion latency/success under real prompt sizes and
   load. A route can ace a 1-token ping and still choke on a 4k-token
   completion.
3. The "no live gateway calls in the request path" rule is about the
   *data plane* (the `/api/v1/preflight` endpoint itself), not the
   control plane — this module runs on the same footing as
   `route_normalizer.py`'s adapter syncs. So skipping active probing is
   a deliberate cost/quality tradeoff, not something forced by that
   rule.

`compute_route_health(agg_row)` is a pure function (zero I/O, easily
unit-tested — see `tests/test_intelligence_rollup.py`) that turns one
row of `queries.fetch_observation_aggregates()` (windowed 5m/1h/24h
success rates, p50/p95 latency, observed cost, and a 24h observation
count, computed in Postgres via `FILTER (WHERE ...)` clauses so it's one
scan of `route_observations`) into a health dict. Two scores come out of
it, and they mean different things:
- `availability` / `health_score` describe *how good the route looks
  right now* — availability prefers the tightest window with data (5m,
  falling back to 1h, then 24h, then defaulting to 1.0 for a route with
  zero observations, since an unobserved route shouldn't be punished for
  silence), and `health_score` further discounts that by the current 5m
  error rate.
- `confidence` describes *how much we trust that number*, separately
  from how good it looks — it's `availability` scaled by
  `min(1.0, observation_count_24h / FULL_CONFIDENCE_OBSERVATIONS)`
  (`FULL_CONFIDENCE_OBSERVATIONS = 50`), so a route with 2 perfect
  observations scores low confidence even though its availability is
  1.0. This is what the engine now reads.

`run_intelligence_rollup_once()` wraps each route's upsert-and-cache work
in its own `try/except` — one bad route (bad data, a transient DB error)
must never abort the rollup for every other route, same defense-in-depth
principle as `route_normalizer.py`'s per-route error handling
(RESOLVED BUG #3 above). The Redis health cache key is
`route:health:<route_id>` with no TTL (`ttl_seconds=None`) — the rollup
loop itself (`run_intelligence_rollup_loop`, default `interval_seconds=20`)
is what keeps it fresh, not Redis expiry.

**New endpoint `POST /api/v1/observations`** (`api/router.py`) — takes
the new `ObservationIn` schema (`schemas/api_models.py`) and calls
`queries.record_observation()`, which inserts into
`route_observations`. This is Preflight's *only* source of real
telemetry: Preflight never executes requests itself (see the two
non-negotiable design properties at the top of this file), so it has no
other way to know whether a route actually worked. `queries.py` gained
`record_observation`, `fetch_observation_aggregates`, and
`upsert_route_intelligence` alongside their SQL constants, following the
existing "all raw SQL lives in `persistence/queries.py`" convention.

**Engine change (`services/preflight_engine.py`):**
`_estimate_confidence(route, health)` now takes the health dict for the
route it's scoring and returns `health["confidence"]` when present,
falling back to the old flat 0.8/0.4 heuristic only when no observations
exist yet for that route. `decide()` already looked up
`route:health:<route_id>` per candidate for latency — that same dict is
now stashed in a `health_by_route_id` map so it can be reused for the
winning route's confidence without a second Redis round-trip.

**Honest caveat:** `route_intelligence` and `route:health:*` stay
completely empty — and the engine keeps using the flat 0.8/0.4 heuristic
— until something actually calls `POST /api/v1/observations` with real
outcomes. Nothing in this repo does that yet. `integration_example.py`
shows (commented out, since that file is illustrative-only) where the
call belongs: right after the existing optimizer/executor actually runs
the selected route, wrapped in a try/except that swallows and logs any
failure so telemetry reporting can never affect the customer's response.
Wiring that call for real is the existing (separate) optimization
layer's job, outside this repo — until that happens,
`run_intelligence_rollup_once` will run and log `Intelligence rollup
updated 0 routes` every cycle, which is expected, not a bug.

### Batched the per-route Redis reads, and actually started the background jobs

Two problems surfaced once pricing + health lookups were both landed:
`decide()` was doing up to 2 sequential Redis round trips (`route:pricing:*`,
`route:health:*`) for EVERY eligible route inside its `for route in
eligible:` loop. With OpenRouter alone exposing 30+ chat routes, that's
40-60+ sequential `GET`s against a `decision_timeout_ms` fail-open budget
of 10ms — each round trip only needs to add sub-millisecond latency for
that budget to blow, and the failure mode is silent: you just get more
`fallback_existing_route` decisions with `"reason": "Preflight failed"`,
which looks exactly like RESOLVED BUG #1 all over again if you don't
know to look at Redis round-trip count. Separately, `run_catalogue_sync_loop`
(`workers/scheduler.py`) and `run_intelligence_rollup_loop`
(`workers/intelligence_rollup.py`) were fully implemented but nothing
ever called them outside of `bootstrap_openrouter.py`'s one-shot sync —
so in a real running app, routes would never re-sync after the initial
bootstrap and `route_intelligence` would never update past whatever a
manual script run produced.

**Batching (`core/redis_cache.py` + `services/preflight_engine.py`):**
added `RedisCache.get_json_many(keys)`, which prefixes every key and
calls `MGET` once instead of `GET` per key, returning a dict keyed by
the *unprefixed* keys the caller passed in (same ergonomics as
`get_json`, callers never think about `<env>:` prefixing). `decide()`
now builds `pricing_map` and `health_map` with two `get_json_many()`
calls total — right before the per-route loop, not inside it — instead
of two `get_json()` calls per route. Empty `keys` short-circuits to `{}`
without touching Redis at all (relevant for the `_no_route_decision`
path, which never reaches this code anyway, but keeps the method safe
to call with zero eligible routes too). This turns O(routes) round trips
into a constant 2, regardless of how many candidate routes a request has.

**Actually starting the background jobs (`api/lifespan.py`):**
`init_preflight_connections` now, after creating the db pool and Redis
client: loads active gateway rows (`queries.load_active_gateways`, new
`LOAD_ACTIVE_GATEWAYS_SQL` — `status != 'disabled'`), builds a
`RedisCache`, and starts both `run_catalogue_sync_loop` and
`run_intelligence_rollup_loop` as `asyncio.create_task(...)`, storing
both tasks in `app.state.preflight_background_tasks`. This whole block
is wrapped in a `try/except` that logs a warning (with `exc_info=True` —
same "never swallow silently" practice as `fail_open.py` and
`route_normalizer.py`) and falls back to `app.state.preflight_background_tasks
= []` on failure, rather than crashing app startup — a fresh environment
where the migration hasn't been applied yet, or `bootstrap_openrouter.py`
hasn't been run yet, should still boot fine and just serve
`no_route_found` decisions from an empty registry, not crash-loop.
`close_preflight_connections` now cancels every task in
`app.state.preflight_background_tasks` (and awaits each one, swallowing
the expected `asyncio.CancelledError`) *before* closing the db pool and
Redis client — so a task doesn't get caught mid-query against a pool
that's already closing.

**`scripts/bootstrap_openrouter.py` is intentionally unaffected** — it
still builds its own pool/Redis/`RouteNormalizer` by hand and calls
`run_catalogue_sync_once` directly, independent of `lifespan.py`. That's
by design: it's meant to be runnable once, standalone, before the app
(and therefore `lifespan.py`) has ever started, to seed the first batch
of routes.

**Tests:** `tests/test_redis_cache_batching.py` uses a fake Redis client
with only an async `mget(keys)` method backed by an in-memory dict and a
call counter — asserts fetching 3 keys (2 present, 1 missing) returns
the right dict keyed by the original unprefixed keys with `None` for the
miss, and that `mget` was called exactly once; and asserts an empty key
list makes zero calls.

### RESOLVED BUG #4 — decision cache ignored inline per-request constraints

**Symptom:** found during live testing after the caching/rollup work
above. Two requests with the identical `tenant_id` + `model` +
`policy_profile_id` but *different* inline `constraints` in the request
body (e.g. one plain, one with `constraints: {"allowed_providers":
["made-up-provider"]}`) could return the exact same cached decision —
the second request's constraints were silently never evaluated if a
cached entry from the first request was still within its 120s TTL. This
isn't hypothetical: it's exactly the sequence in `MENTOR_GUIDE.md`'s
Demo 1 (no constraints) followed immediately by Demo 2 (`allowed_providers:
["made-up-provider"]}`) — running Demo 2 within 120s of Demo 1 against
the pre-fix code could return Demo 1's cached `"decision": "route"`
instead of the documented `"decision": "fallback_existing_route"` with
`provider_not_allowed` rejections.

**Root cause:** `PreflightEngine._decision_cache_key(profile)` only
hashed `profile.tenant_id`, `profile.requested_model`, and
`profile.policy_profile_id` — all pulled from `RequestProfile`
(`schemas/internal_models.py`), which never carried the raw
`PreflightRequest.constraints` in the first place (`_build_profile()`
only spreads two constraint fields — `allowed_regions[0]` and
`data_classification` — into `RequestProfile.region` /
`.data_classification`; everything else, including `allowed_providers`,
`allowed_models`, `max_cost_usd`, `max_latency_ms`,
`required_capabilities`, was dropped entirely before the cache key was
ever computed).

**Fix applied:** rather than growing `RequestProfile` to carry the full
constraints blob (which would mean keeping two representations of the
same data in sync), `_decision_cache_key` now takes the raw
`PreflightConstraints | None` straight from `request.constraints` as a
second parameter, alongside `profile`. It hashes
`json.dumps(constraints.model_dump(), sort_keys=True, default=str)` (or
`json.dumps({})` when `constraints is None`) into the same `raw` string
that already had tenant/model/policy_profile_id. `sort_keys=True` keeps
the hash stable regardless of field insertion order. The call site in
`decide()` changed from `self._decision_cache_key(profile)` to
`self._decision_cache_key(profile, request.constraints)` — `request` was
already in scope there, so this needed no new plumbing through `decide()`
itself.

**New candidate metadata (`schemas/api_models.py` +
`services/preflight_engine.py` + `services/route_filter.py`):** found
alongside the cache-key bug during the same live-testing pass —
`CandidateRoute` entries in API responses showed `route_id`/cost/latency/
status but never `provider`, `model`, or `capabilities`, making a
30+-entry `candidates` list unreadable without a separate DB lookup to
map `route_id` back to something human-legible. Added `provider: Optional[str]`,
`model: Optional[str]`, and `capabilities: Optional[List[str]]` to
`CandidateRoute` (all optional so nothing existing breaks), and populated
them at both construction sites: the eligible-routes loop in
`preflight_engine.py::decide()`, and the rejected-routes construction in
`route_filter.py::hard_filter()` (via `route.get(...)`, since a rejected
route's dict still has these fields from `load_candidate_routes`).

**Tests:** `tests/test_decision_cache_key.py` is the load-bearing proof
for this bug — it calls `_decision_cache_key` directly with the same
`RequestProfile` but different `PreflightConstraints` (none vs.
`allowed_providers=["made-up-provider"]`) and asserts the two resulting
keys differ; also checks two different non-empty constraint sets differ
from each other, identical constraints hash identically, and `None` is
stable across repeated calls. This test would fail against the pre-fix
single-argument `_decision_cache_key(profile)` — there was no way to
feed it different constraints and get different output, since it never
looked at constraints at all.

**`MENTOR_GUIDE.md` Demo 2 needs no changes** — its curl example already
sends distinct constraint bodies per demo; it was relying on correct
behavior that the bug above was violating. With the fix, running Demo 1
immediately followed by Demo 2 (or in either order, or repeated) now
always evaluates each request's constraints independently, exactly as
the doc already describes — no doc update needed, this was a bug in the
engine, not a doc that oversold a broken feature.

---

## Session 2 complete

Every file touched across this session's four passes (Redis caching →
observation/intelligence rollup → Redis MGET batching + background job
startup → decision-cache-key bug + candidate metadata), for anyone
wanting the full diff summary without re-deriving it from git log:

- `core/config.py` — `policy_cache_ttl_seconds`, `pricing_cache_ttl_seconds`
- `core/redis_cache.py` — `get_json_many()` (MGET batching)
- `persistence/queries.py` — cache-through `load_policy()`;
  `record_observation`, `fetch_observation_aggregates`,
  `upsert_route_intelligence`, `load_active_gateways` + their SQL
- `workers/route_normalizer.py` — `RETURNING route_id`, `fetchval`,
  optional `redis_cache` param, writes `route:pricing:<id>` on upsert
- `workers/scheduler.py` — optional `redis_cache` threaded through both
  sync functions
- `workers/intelligence_rollup.py` — **new file**: `compute_route_health`,
  `run_intelligence_rollup_once`/`_loop`
- `services/preflight_engine.py` — cache-through policy lookup, pricing
  cache read with Postgres fallback, batched MGET pricing/health lookups,
  real `_estimate_confidence` from route_intelligence, fixed
  `_decision_cache_key` to include inline constraints, populated new
  `CandidateRoute` metadata fields
- `services/route_filter.py` — populated new `CandidateRoute` metadata
  fields on rejected routes
- `schemas/api_models.py` — new `ObservationIn`; `CandidateRoute` gained
  `provider`/`model`/`capabilities`
- `api/router.py` — new `POST /api/v1/observations` endpoint
- `api/lifespan.py` — starts + tracks + cancels the catalogue sync and
  intelligence rollup background loops
- `scripts/bootstrap_openrouter.py` — builds its own `RedisCache`,
  independent of `lifespan.py`, to also warm the pricing cache
- `integration_example.py` — illustrative commented call to
  `POST /api/v1/observations` after route execution
- New tests: `test_policy_cache.py`, `test_intelligence_rollup.py`,
  `test_redis_cache_batching.py`, `test_decision_cache_key.py`

Full `pytest -v` suite: 26 passed, 0 skipped, no live Postgres/Redis
required for any of them (all are pure-function or fake-collaborator
unit tests). `python3 -m py_compile` across all of `app/` and `scripts/`:
zero syntax errors.

## Session 3 — LiteLLM adapter (second real gateway)

Added `adapters/litellm_adapter.py`, a second `GatewayAdapter`
implementation, closing part of the "Not built yet" gap noted at the top
of this file (Kong is still not built — deliberately out of scope for
this session).

**Why this is real data, not a mock:** LiteLLM doesn't need a live proxy
deployment to get real routes from — it publishes its entire
model/pricing catalog as a static, public, no-auth JSON file in its own
GitHub repo:
`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`.
That's the same file every LiteLLM proxy instance ships bundled with.
Fetching it is a real HTTP call against real, current, third-party data
— not hand-written fixtures — so once LiteLLM's routes sit in the same
`routes` table as OpenRouter's, the engine's cross-gateway cost
comparison is proving something genuine, not just exercising code paths
against one data source.

**Verified against the live catalog before writing the mapper** (per
the task's instruction not to assume the shape) — fetched the real file
and inspected it with a throwaway script rather than trusting the
assumed shape:
- 3,111 top-level keys. `mode` values found in the wild: `chat` (2,380),
  `image_generation` (215), `embedding` (132), `responses` (89),
  `audio_transcription` (66), `completion` (36), `audio_speech` (31),
  `image_edit` (31), `realtime` (29), `rerank` (25), `video_generation`
  (25), `search` (20), `ocr` (14), `None`/missing (9), `moderation` (6),
  `guardrail` (1), `vector_store` (1) — considerably more non-chat
  modes mixed into one flat file than the task description enumerated,
  which only strengthens the case for the `mode in ("chat",
  "completion")` filter being load-bearing, not optional. This is the
  same class of problem already documented in **RESOLVED BUG #3** above
  (a non-chat OpenRouter model with non-token pricing sailing through
  unfiltered and overflowing the `numeric(12,6)` price column) —
  LiteLLM's catalog just mixes far more non-chat modes into one file
  than OpenRouter's `/models` response does, so the same filter matters
  even more here.
- `sample_spec` is confirmed real and present — a documentation-template
  entry whose field values are description *strings* (e.g. `"mode": "one
  of: chat, embedding, completion, ..."`), not real model data. It would
  already fail the mode filter, but the mapper checks the literal key
  first for a clearer skip reason.
- 503 of 3,111 entries are missing `input_cost_per_token` and/or
  `output_cost_per_token` entirely (confirmed via `.get()`, not
  indexing) — real incomplete/placeholder rows, not a hypothetical.
- **The `MAX_REASONABLE_PRICE_PER_1M` sanity cap is not just defensive
  copy-paste from OpenRouter's adapter — it's necessary.** 5 real
  entries in the live catalog (all under the `wandb/` provider prefix,
  e.g. `wandb/deepseek-ai/DeepSeek-R1-0528` at 135,000/1M input and
  540,000/1M output) genuinely exceed the 100,000-per-1M cap after
  conversion. Confirmed bad upstream data, not a hypothetical edge case.
- `litellm_provider` was present on every chat/completion entry checked
  (0 missing) — the `.get("litellm_provider", "unknown")` fallback in
  the mapper is defensive-only in practice, not covering an observed gap.
- `supports_function_calling` was present on only 1,685 of 2,416
  chat/completion entries (~70%) — confirms the `.get(...)` (not
  indexing) is required, not optional, for that field.
- Ran the mapper against the full real 3,111-entry catalog end-to-end
  (not just the unit tests' small fixtures) as a final live smoke test:
  **2,295 routes mapped successfully, 816 skipped (non-chat modality,
  missing cost fields, or implausible price), zero crashes.**

**`map_litellm_model_to_supervea(model_key, model_obj)`** — standalone,
pure, unit-testable function (mirrors `map_openrouter_model_to_supervea`'s
shape exactly): skips `sample_spec` by name, skips anything whose `mode`
isn't `"chat"` or `"completion"`, skips entries missing either cost
field, applies the same `MAX_REASONABLE_PRICE_PER_1M` cap OpenRouter's
mapper uses, builds `provider = litellm_provider` and `model =
"{provider}:{model_key}"` (same `provider:model` convention as
OpenRouter, so both gateways' routes are comparable/joinable in the DB
by model string), converts cost-per-token to cost-per-1M with the same
rounding convention as `_to_per_million()` (a local copy, not a
cross-file import, to keep each adapter file self-contained), and adds
`"tool_use"` / `"long_context"` capabilities the same way OpenRouter's
mapper does (`supports_function_calling` / `max_input_tokens >=
128_000`). `region="global"`, `data_regions=["US", "EU"]` — LiteLLM's
catalog doesn't specify regions, so these match OpenRouter's same
defaults for consistency.

**`LiteLLMAdapter(GatewayAdapter)`** — `discover_routes()` fetches the
catalog once via `httpx`, iterates its top-level items, and wraps each
`map_litellm_model_to_supervea()` call in the exact same `(KeyError,
ValueError, TypeError)` per-entry `try/except` as
`OpenRouterAdapter.discover_routes()` — one bad/non-chat/implausible
entry is skipped and logged, never aborts the sync (same defense-in-depth
principle as RESOLVED BUG #3's fix). `get_health()` does a cheap
low-timeout re-fetch of the catalog, same `httpx.HTTPError` pattern as
OpenRouter's. `get_usage`/`get_pricing`/`get_metadata` are `{}` MVP
stubs, same as OpenRouter's. `gateway_config` only needs `catalog_url`
(defaults to the GitHub raw URL, `DEFAULT_CATALOG_URL`) and `timeout_s`.

**Wiring:** `workers/scheduler.py`'s `ADAPTER_REGISTRY` gained
`"litellm": LiteLLMAdapter` alongside the existing `"openrouter"` entry
— no other change to `scheduler.py` was needed, since
`run_catalogue_sync_once()`'s generic adapter-construction path already
works for LiteLLM (it just ignores the OpenRouter-specific
`api_key`/`base_url` config keys it's handed and falls back to
`DEFAULT_CATALOG_URL`).

**New `scripts/bootstrap_litellm.py`** — an exact mirror of
`bootstrap_openrouter.py`'s structure (same `FIND_GATEWAY_SQL` /
`INSERT_GATEWAY_SQL` pattern, same existing-row check before inserting,
same pool/Redis/`RouteNormalizer` wiring via
`run_catalogue_sync_once(..., redis_cache=redis_cache)`), inserting a
gateway row with `name="LiteLLM"`, `type="litellm"`, `endpoint=
DEFAULT_CATALOG_URL`. `bootstrap_openrouter.py` itself was **not**
touched — the two scripts are fully independent and both idempotent
(safe to re-run; each checks for its own gateway row by name first).
Run it with:
```bash
python -m scripts.bootstrap_litellm
```

**Tests:** `tests/test_litellm_mapping.py` (9 tests, mirrors
`test_estimators_and_mapping.py`'s OpenRouter mapping tests in
structure) — basic chat model maps correctly (provider/model/
capabilities/price conversion), `mode="embedding"` and
`mode="image_generation"` both raise `ValueError` (the direct equivalent
of OpenRouter's modality-filter protection — the most important test
here, same as the task called out), `sample_spec` is skipped, an
implausible price derived from `MAX_REASONABLE_PRICE_PER_1M + 1` raises,
`supports_function_calling=True`/absent correctly toggles `tool_use`,
and `mode="completion"` (not just `"chat"`) is accepted. All 9 pass with
no live infrastructure — pure-function tests against literal dicts.

Full `pytest -v` after this session: 35 passed (26 from Session 2 +
9 new), 0 skipped. `python3 -m py_compile` across every new/changed
file: zero syntax errors.

## Session 4 — RESOLVED BUG #5 — inline request.constraints never reached hard_filter()

**Symptom:** found during live testing, and looked at first glance like a
recurrence of RESOLVED BUG #4 (the decision-cache-key bug) — a request
sent with `constraints: {"allowed_providers": ["made-up-provider"]}` and
no `policy_profile_id` came back `"decision": "route"` with every
candidate `"status": "eligible"`, when it should have fallen back with
every candidate rejected `provider_not_allowed`. The live curl that first
exposed it:

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}],
       "constraints": {"allowed_providers": ["made-up-provider"]}}'
```
Expected after this fix: `"decision": "fallback_existing_route"`, every
candidate's `rejection_reason` == `"provider_not_allowed"`. Re-run this
exact command to re-verify the fix in a future session.

**Why it looked like the cache-key bug but wasn't:** BUG #4's fix already
made `_decision_cache_key` hash `request.constraints`, so a fresh
(uncached) request should never have been able to silently reuse another
request's decision — and indeed it wasn't a caching artifact. Re-running
the curl above repeatedly, well past the 120s cache TTL, reproduced the
same wrong `"decision": "route"` result every time. That ruled out the
cache and pointed at the filtering path itself.

**Root cause:** `request.constraints` never reached `route_filter.hard_filter()`
at all — regardless of caching. Tracing `decide()` step by step:
`_build_profile(request)` builds a `RequestProfile`
(`schemas/internal_models.py`), and `RequestProfile` has no field for the
constraints object itself — it only has `policy_profile_id` (a string
reference) and, separately, `region` / `data_classification`, which
`_build_profile()` populates by reading exactly two fields off
`request.constraints` (`allowed_regions[0]` and `data_classification`).
Every other field on `PreflightConstraints` — `allowed_providers`,
`allowed_models`, `max_cost_usd`, `max_latency_ms`,
`required_capabilities` — was read from `request.constraints` **nowhere**
in the engine. `decide()` then computed `policy = await
queries.load_policy(self.db, profile.policy_profile_id, self.redis)`,
which only ever looks up a *stored* policy_profile row (or returns an
empty `PreflightConstraints()` if `policy_profile_id` is `None`, which it
was in the reported curl — no stored policy was even configured), and
passed that `policy` straight into `hard_filter()`. Inline constraints on
the request body were silently discarded before hard_filter ever saw
them — a "no policy" request effectively had zero policy enforcement,
inline constraints or not.

**Fix applied:** added `services/policy_merge.py::merge_constraints(base,
override)`, a pure function (no I/O, so no engine/DB/Redis needed to unit
test it) that combines a stored policy (`base`) with inline request
constraints (`override`) into one `PreflightConstraints`, "most
restrictive of each field wins" — the result can never be looser than
either input alone:
- `allowed_providers` / `allowed_models` / `allowed_regions` /
  `required_capabilities` (all `Optional[List[str]]`): if both sides are
  set, the result is the **set intersection** (deduped list — a provider
  must be allowed by *both* sides to remain allowed); if only one side is
  set, use it unchanged; if neither, `None` ("unrestricted", matching
  `hard_filter`'s existing `if policy.allowed_providers and ...` falsy-`None`
  check). An intersection that comes out empty (e.g. disjoint lists) is
  intentional and correct — it means "no provider satisfies both", which
  `hard_filter` then correctly turns into `no_route_found` /
  `fallback_existing_route` downstream, not a crash.
- `max_cost_usd` / `max_latency_ms` (`Optional[float]`/`Optional[int]`):
  if both set, `min(base, override)` (the tighter cap wins); if only one
  set, use it; if neither, `None`.
- `data_classification` (defaults to `"internal"`, not `None`): explicit
  ordering `public < internal < sensitive < restricted`; result is
  whichever side is more restrictive, regardless of whether that's `base`
  or `override` — e.g. a stored policy of `"sensitive"` stays
  `"sensitive"` even if the inline request only asks for `"public"`, and
  an inline request asking for `"restricted"` tightens a stored
  `"internal"` policy up to `"restricted"`.

`services/preflight_engine.py::decide()` now does, immediately after the
existing `load_policy` call: `policy = merge_constraints(policy,
request.constraints)` — before `policy` is used for `hard_filter()` or
the `max_cost_usd`/`max_latency_ms` soft-constraint check
(`_within_constraints`) later in the same method, so *both* filtering
stages see the merged, correctly-restrictive policy, not just the stored
one. `request` (the full `PreflightRequest`) was already in scope at that
point in `decide()` — no new plumbing needed, same as BUG #4's fix.

**Decision-cache key needed no change** — confirmed, not assumed: BUG
#4's fix already hashes the raw `request.constraints` object directly
into `_decision_cache_key`, independent of how `policy` itself gets
computed downstream, so it already distinguishes any two requests with
different inline constraints regardless of this bug or its fix.

**`RequestProfile` was deliberately NOT expanded to carry the full
constraints blob** — same reasoning BUG #4's fix used for the cache key:
keeping the raw `PreflightConstraints` and its `RequestProfile`
projection in sync as two representations of the same data is a future
bug waiting to happen. `merge_constraints` instead takes the raw
`request.constraints` straight from the `PreflightRequest` still in scope
inside `decide()`.

**Tests:**
- `tests/test_policy_merge.py` — pure unit tests of `merge_constraints`
  directly (no engine, no DB/Redis): disjoint `allowed_providers` produce
  an empty (not crashing) intersection; overlapping lists intersect
  correctly; only-override-set passes the override value through
  unchanged; `max_cost_usd` takes whichever side is smaller in both
  directions (override-smaller and base-smaller); `data_classification`
  picks the more restrictive side regardless of which side (base or
  override) it came from; `override=None` returns `base` unchanged
  (byte-for-byte backward compatible with the pre-fix "no inline
  constraints" case).
- `tests/test_inline_constraints_integration.py` — the load-bearing proof
  this bug is actually fixed, at the `PreflightEngine.decide()` level,
  not just at the pure-function level: builds a `PreflightRequest` with
  `constraints=PreflightConstraints(allowed_providers=["made-up-provider"])`
  and no `policy_profile_id`, runs it through `decide()` against two fake
  healthy/fresh routes (`provider="openai"`, `provider="anthropic"`) using
  in-memory `FakeDB`/`FakeRedisCache` stand-ins (same pattern as
  `test_policy_cache.py` — no live Postgres/Redis), and asserts
  `decision.decision == "fallback_existing_route"` with **every**
  candidate's `rejection_reason == "provider_not_allowed"`. This test
  fails against the pre-fix code (it returns `"decision": "route"` with
  both candidates `"status": "eligible"`) and passes after the fix — it's
  the direct regression test for the reported curl.

Full `pytest -v` after this session: 45 passed (35 from Session 3 + 9 new
`test_policy_merge.py` + 1 new `test_inline_constraints_integration.py`),
0 skipped, no live Postgres/Redis required. `python3 -m py_compile`
across every new/changed file: zero syntax errors.

## Session 5 — top-level `capabilities` on PreflightDecision

`CandidateRoute.capabilities` (added in Session 4 alongside `provider`/
`model`) only existed inside the `candidates` array — reading the
*winning* route's capabilities required cross-referencing
`decision.route_id` against `decision.candidates` to find the matching
entry. Added `capabilities: Optional[List[str]] = None` directly to
`PreflightDecision` (`schemas/api_models.py`), right next to the existing
top-level `model` field. **This is not a blueprint/spec requirement** —
it's a pure readability improvement layered on top of the Session 4 work,
mirroring the same `provider`/`model` pattern that already existed at the
top level for the same reason.

`services/preflight_engine.py::decide()` now sets
`capabilities=selected_route.get("capabilities")` in the successful-route
`PreflightDecision(...)` construction, alongside the existing
`provider=selected_route["provider"]` / `model=selected_route["model"]`.
`_no_route_decision()` now explicitly sets `capabilities=None` (would
default to `None` anyway, but explicit here for the same reason
`route_id`/`provider`/`model` are conceptually absent on that path — makes
the intent readable at the call site). The `candidates` list construction
itself (`route_filter.py` and the eligible-routes loop in the engine) was
untouched — that part already populated per-candidate `capabilities`
correctly in Session 4; this session only mirrors that value up to the
top level for the winning route.

**Test:** `tests/test_top_level_capabilities.py` — two fake routes with
different capability lists (`["chat", "tool_use"]` vs. `["chat",
"long_context"]`), asserts the cheaper route wins, `decision.capabilities`
is not `None`, and it equals the `capabilities` of whichever candidate in
`decision.candidates` has the matching `route_id` — proving the top-level
field and the per-candidate entry actually agree, not just that the field
exists. A second test asserts the `fallback_existing_route` path has
`decision.capabilities is None`.

Full `pytest -v` after this session: 47 passed (45 from Session 4 + 2
new in `test_top_level_capabilities.py`), 0 skipped, no live
Postgres/Redis required. `python3 -m py_compile` on both changed files:
zero syntax errors.
