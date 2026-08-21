# Supervea Preflight MVP — Mentor Walkthrough

## The Goal

Right now, Supervea sits in front of an AI gateway (OpenRouter, Kong, LiteLLM) and
optimizes traffic **after** the gateway has already decided which model to use. The
problem: each gateway only knows about routes within itself — none of them compare
across each other.

**The idea:** build a thin layer that sits *above* all of them, looks at every
available route across every gateway, and picks the cheapest, healthiest,
policy-compliant one — **before** the request even reaches the existing optimization
logic.

**The constraint that shapes everything:** it has to be safe to bolt onto a system
with real paying customers on day one. Two rules are baked into every part of the
design:

1. **It must never break existing traffic.** If it fails, times out, or has a bug,
   the system falls back to exactly what it does today.
2. **It must be provably right, not "AI-vibes right."** Every decision is a
   deterministic calculation — cost math plus rule checks — never a model guessing.
   That means every decision can be logged, replayed, and explained.

One sentence version: **a safety-netted, explainable router-of-routers.**

---

## The Big Picture — How Data Moves

Two separate systems, running at different speeds:

**Background system** (runs every few minutes, nobody's waiting on it) — goes out to
OpenRouter's real API, asks "what models exist and what do they cost right now," and
saves that list into a database. This is the *inventory*.

**Live system** (runs in milliseconds, a real user is waiting) — when a request comes
in, it does **not** call OpenRouter live, that would be too slow. It reads the
inventory already saved in the database, filters out anything unhealthy or against
policy, does simple math to estimate cost, and picks the cheapest valid option.

This split exists purely for speed: talking to an external API takes hundreds of
milliseconds; reading from your own database takes single digits.

---

## Walking Through It, One Piece at a Time

### 1. The Inventory — What Routes Exist

**File: `adapters/openrouter_adapter.py`**

```python
async def discover_routes(self) -> AsyncIterator[DiscoveredRoute]:
    yield build_auto_router_route()
    response = await self._get("/models")
    for item in response.get("data", []):
        yield map_openrouter_model_to_supervea(item)
```

This is the piece that talks to the outside world. It calls OpenRouter's `/models`
endpoint (a real HTTP request), gets back a list like `openai/gpt-4o-mini`,
`anthropic/claude-3-sonnet`, and converts each one into Supervea's own format.

```python
def map_openrouter_model_to_supervea(model_obj: dict) -> DiscoveredRoute:
    or_id = model_obj["id"]                       # 'openai/gpt-4o-mini'
    provider, raw_model = or_id.split("/", 1)      # 'openai', 'gpt-4o-mini'
    supervea_model = f"{provider}:{raw_model}"     # 'openai:gpt-4o-mini'
```

OpenRouter names things one way, we rename it into a consistent internal format.
Every gateway (Kong, LiteLLM, later) would map into this **same** shape — that's what
lets the decision engine compare routes from completely different sources as if they
were equivalent.

*When `python -m scripts.bootstrap_openrouter` was run, this file is what executed —
it pulled the real list of models from OpenRouter and saved them into Postgres.*

### 2. The Inventory's Home — The Database

**File: `migrations/001_preflight_schema.sql`**

The table that matters most is `routes`. Every row is one model on one gateway:

```sql
CREATE TABLE public.routes (
    route_id uuid PRIMARY KEY,
    provider text,               -- 'openai'
    model text,                  -- 'openai:gpt-4o-mini'
    input_price_per_1m numeric,  -- cost per 1M input tokens
    output_price_per_1m numeric,
    status text,                 -- 'healthy' / 'unhealthy'
    data_freshness text          -- 'fresh' / 'stale'
);
```

This is the "menu" the decision engine picks from. `status` and `data_freshness` are
the two fields checked first in the filter step below.

### 3. Writing New Data Without Duplicating — The Normalizer

**File: `workers/route_normalizer.py`**

```sql
INSERT INTO public.routes (...) VALUES (...)
ON CONFLICT (gateway_id, provider, model, region)
DO UPDATE SET ... status = EXCLUDED.status, data_freshness = 'fresh';
```

Every time the background sync runs, it doesn't create duplicate rows for the same
model — it either inserts fresh or updates the existing row's price/status.

```python
async def sync_gateway_routes(self, gateway_row, adapter) -> int:
    try:
        async for discovered in adapter.discover_routes():
            await self._upsert_route(gateway_row, discovered)
    except Exception:
        await self._mark_gateway_routes_stale(gateway_row["gateway_id"])
```

If OpenRouter's API is down when this job runs, nothing crashes and no data is
deleted — routes just get marked `stale`. A stale route gets excluded later, so a
dead upstream API degrades gracefully instead of poisoning decisions with old prices
pretending to be current.

### 4. A Request Comes In — The Live Endpoint

**File: `api/router.py`**

```python
@router.post("/preflight", response_model=PreflightDecision)
async def preflight_route(body: PreflightRequest, ...):
    return await safe_decide_with_timeout(engine, body, mode=settings.mode)
```

This is the URL hit with `curl`. It takes the JSON body (model requested, messages,
optional constraints) and hands it to the engine — wrapped in a safety net (step 8).

### 5. Filtering Out Bad Options — The Rule Checker

**File: `services/route_filter.py`**

```python
def _evaluate(self, route, policy) -> str | None:
    if route["status"] != "healthy":
        return "route_status_unhealthy"
    if route["data_freshness"] == "stale":
        return "route_data_stale"
    if policy.allowed_providers and route["provider"] not in policy.allowed_providers:
        return "provider_not_allowed"
    return None
```

Reads top to bottom: is it healthy? Is the data current? Is the provider allowed?
Every route in the inventory goes through this one at a time. Anything that fails
gets a specific reason attached — that reason is what shows up in the API response.

### 6. Doing the Math — Cost Estimation

**File: `services/estimators.py`**

```python
def estimate_cost_usd(self, route, input_tokens, output_tokens) -> float:
    in_price = float(route["input_price_per_1m"])
    out_price = float(route["output_price_per_1m"])
    return (input_tokens / 1_000_000.0) * in_price + (output_tokens / 1_000_000.0) * out_price
```

Just arithmetic — no AI, no guessing. Every route that survives the filter gets a
cost number attached this way.

### 7. Making the Actual Decision — The Sort

**File: `services/preflight_engine.py`**

```python
constrained.sort(key=lambda rc: (rc[1].estimated_cost_usd, rc[1].estimated_latency_ms))
selected_route, selected_candidate = constrained[0]
```

This one line is the entire "brain." Sort every surviving, priced route by
`(cost, then latency as tiebreaker)`, take whichever comes first. The "decision" is
just picking the top of a sorted list.

### 8. The Safety Net — Never Break Real Traffic

**File: `services/fail_open.py`**

```python
try:
    return await asyncio.wait_for(engine.decide(request, mode=mode), timeout=budget_ms / 1000.0)
except Exception:
    return PreflightDecision(decision="fallback_existing_route", ...)
```

Everything in steps 5–7 happens inside `engine.decide()`. This function wraps that
call in a strict time limit and catches any possible failure — a slow database, a
bug, the DB being completely down. If anything goes wrong, it returns a safe
"just use the existing route" answer instead of crashing.

### One-Sentence Summary Table

| Piece | One sentence |
|---|---|
| OpenRouter adapter | Pulls the live list of AI models and prices from OpenRouter |
| Database (`routes` table) | Stores that list so we don't call OpenRouter on every request |
| Route normalizer | Keeps the stored list updated, safely, even if OpenRouter is down |
| API endpoint | The door a request walks through |
| Route filter | Throws out anything unhealthy, stale, or against policy — with a reason |
| Estimators | Does the cost math for whatever's left |
| Decision engine (the sort) | Picks the cheapest option among what survived |
| Fail-open wrapper | Guarantees the system never breaks, even when something fails |

---

## Live Demo — curl Commands to Run Yourself

Run these in a **second terminal tab** while `uvicorn app.main:app --reload --port 8001`
is running in the first.

### 1. Happy path

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'
```

**What it tests:** the full pipeline end to end — load routes from the DB, filter,
estimate cost, sort, pick a winner.

**Expect:** `"decision": "route"`, a real `model` like `"openai:gpt-4o-mini"`, an
`estimated_cost_usd`, and a `candidates` list.

---

### 2. Provider constraint that excludes everything

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}], "constraints": {"allowed_providers": ["made-up-provider"]}}'
```

**What it tests:** the rule checker in `route_filter.py` — specifically the
`provider_not_allowed` branch.

**Expect:** `"decision": "fallback_existing_route"`, and every entry in `candidates`
shows `"status": "rejected"` with `"rejection_reason": "provider_not_allowed"`.

**Why this is the best one to show your mentor:** it proves the system doesn't
silently pick something when it shouldn't — every rejection is logged with a reason,
so the whole decision is auditable, not a black box.

---

### 3. Requiring a capability most models don't have

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}], "constraints": {"required_capabilities": ["long_context"]}}'
```

**What it tests:** the `missing_capability` branch of the filter — only models with
128K+ context length (tagged `long_context` during the OpenRouter mapping step)
survive.

**Expect:** `"decision": "route"`, but the selected `model` should differ from
Example 1 — it'll be a model with a larger context window.

---

### 4. Impossibly low cost cap

```bash
curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}], "constraints": {"max_cost_usd": 0.0000001}}'
```

**What it tests:** the "relax rather than refuse" logic in `preflight_engine.py` —
when no route meets a soft constraint (cost/latency caps), the engine falls back to
considering all eligible routes anyway instead of giving up.

**Expect:** still `"decision": "route"` — not a fallback. It picks the cheapest
available option even though it's technically over the requested cap.

---

### 5. Same request twice — proves the cache is real

```bash
time curl -s -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}' > /dev/null

time curl -s -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}' > /dev/null
```

**What it tests:** the Redis decision cache in `preflight_engine.py`
(`self.redis.get_json(cache_key)` short-circuit).

**Expect:** the second call's `real` time should be noticeably lower than the
first — it skipped the Postgres queries entirely and returned the cached decision.

---

### 6. Fail-open — database goes down mid-demo

```bash
brew services stop postgresql@15

curl -X POST http://localhost:8001/api/v1/preflight \
  -H "X-Supervea-Tenant: demo-tenant" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "hello"}]}'

brew services start postgresql@15   # turn it back on afterward
```

**What it tests:** the `except Exception` catch-all in `fail_open.py`.

**Expect:** instead of a 500 error or a hung connection, you still get back a valid
`PreflightDecision` JSON with `"decision": "fallback_existing_route"` and
`"reason": "Preflight failed or timed out; using existing configured route."`

**Why this is the most important one to demo live:** it's the actual proof that this
system cannot break the app it's protecting, even in the worst case — the database
disappearing entirely.

---

## Suggested Demo Order

1. Show the architecture flowchart (30 seconds — gives the mental model).
2. Run Example 1 — the happy path.
3. Run Example 2 — the rejection/audit trail (most convincing single example).
4. Run Example 6 — fail-open live (second most convincing — do this last for impact).
5. Run `pytest -v` in the project to show passing unit tests backing the logic.
6. Mention what's not built yet, honestly: Kong/LiteLLM adapters, the Redis pricing
   and policy caches (currently every decision hits Postgres directly), and the
   health-check polling loop. Framing: "core engine and OpenRouter integration are
   built and tested against the real API; the caching layer that gets this under the
   10ms budget, plus additional gateway adapters, are the next milestones."

---

## A Real Bug Found During Testing (worth mentioning to your mentor)

This is actually good material to show — it demonstrates the testing process working
as intended, catching a real issue before it shipped.

**What happened:** every curl example above initially failed with:

```json
{"detail":[{"type":"missing","loc":["body","tenant_id"],"msg":"Field required",...}]}
```

**Root cause:** `PreflightRequest.tenant_id` was declared as a required field with no
default:

```python
tenant_id: str = Field(description="Tenant / org identifier")  # no default
```

The intent was always for the tenant to come from the `X-Supervea-Tenant` header, not
the JSON body — the endpoint does this:

```python
async def preflight_route(body: PreflightRequest, tenant_id: str = Depends(get_tenant_id), ...):
    body.tenant_id = tenant_id   # overwrite whatever was in the body
```

But FastAPI validates the incoming JSON against the `PreflightRequest` schema
**before** this function body ever runs. Since `tenant_id` had no default value,
Pydantic rejected any request that didn't include a `tenant_id` field in the JSON —
which no curl example did, since it's supposed to come from the header instead.

**Fix:** give `tenant_id` a default value so the schema stops requiring it in the
body. The header still overwrites it immediately after, so the actual security
property (tenant identity comes from the header, not a value the client can fake in
the JSON) is unaffected — it's a validation-timing fix, not a behavior change.

```python
tenant_id: str = Field(
    default="",
    description="Tenant / org identifier. Populated from the X-Supervea-Tenant "
    "header by the endpoint — do not send this in the request body.",
)
```

**Lesson for the mentor conversation:** this is exactly the kind of bug that hand-run
curl testing catches and a "looks right on paper" code review misses — the schema
and the endpoint logic each looked correct in isolation, but the *order* FastAPI
executes them in broke the interaction between the two. All curl examples above
should be re-run after this fix.
