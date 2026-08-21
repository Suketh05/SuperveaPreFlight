-- Supervea Preflight MVP — initial schema
-- Multi-tenant assumed; enable RLS at the Supabase level for tenant-scoped tables.

-- ============================================================
-- gateways: one row per configured gateway/router
-- ============================================================
CREATE TABLE IF NOT EXISTS public.gateways (
    gateway_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text,                    -- optional, for tenant-specific gateways
    name            text NOT NULL,           -- 'OpenRouter', 'Kong-prod', 'LiteLLM-eu-1'
    type            text NOT NULL,           -- 'openrouter', 'kong', 'litellm', 'direct'
    endpoint        text NOT NULL,
    environment     text NOT NULL,           -- 'prod', 'staging', etc.
    region          text NOT NULL,           -- 'us', 'eu', 'global'
    status          text NOT NULL,           -- 'healthy', 'degraded', 'unhealthy', 'disabled'
    last_seen       timestamptz,
    adapter_version text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gateways_type_idx   ON public.gateways(type);
CREATE INDEX IF NOT EXISTS gateways_region_idx ON public.gateways(region);
CREATE INDEX IF NOT EXISTS gateways_tenant_idx ON public.gateways(tenant_id);

-- ============================================================
-- routes: one row per executable route (gateway + provider + model)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.routes (
    route_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    gateway_id              uuid NOT NULL REFERENCES public.gateways(gateway_id) ON DELETE CASCADE,
    provider                text NOT NULL,     -- 'openai', 'anthropic', 'openrouter', etc.
    model                   text NOT NULL,     -- 'openai:gpt-4o-mini', 'openrouter:auto', ...
    model_version           text,
    route_kind              text NOT NULL DEFAULT 'model', -- 'model' | 'router' | 'direct'
    region                  text NOT NULL,
    capabilities            text[] NOT NULL,
    input_price_per_1m      numeric(12, 6) NOT NULL,
    output_price_per_1m     numeric(12, 6) NOT NULL,
    currency                text NOT NULL DEFAULT 'USD',
    advertised_availability numeric(5, 4),
    advertised_latency_p50  integer,
    advertised_latency_p95  integer,
    data_regions            text[] NOT NULL,
    status                  text NOT NULL,     -- 'healthy', 'degraded', 'unhealthy', 'disabled'
    data_freshness          text NOT NULL DEFAULT 'fresh', -- 'fresh', 'stale', 'unknown'
    last_successful_sync    timestamptz,
    last_updated            timestamptz NOT NULL DEFAULT now(),
    created_at               timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS routes_gateway_provider_model_region_uniq
    ON public.routes(gateway_id, provider, model, region);
CREATE INDEX IF NOT EXISTS routes_provider_model_idx ON public.routes(provider, model);
CREATE INDEX IF NOT EXISTS routes_region_idx         ON public.routes(region);
CREATE INDEX IF NOT EXISTS routes_status_idx         ON public.routes(status);

-- ============================================================
-- policy_profiles: policy constraints for tenants / environments
-- ============================================================
CREATE TABLE IF NOT EXISTS public.policy_profiles (
    policy_profile_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             text NOT NULL,
    name                  text NOT NULL,
    allowed_providers     text[] DEFAULT NULL,
    allowed_models        text[] DEFAULT NULL,
    allowed_regions       text[] DEFAULT NULL,
    max_cost_usd          numeric(12, 6),
    max_latency_ms        integer,
    required_capabilities text[] DEFAULT NULL,
    data_classification   text DEFAULT 'internal', -- 'public','internal','sensitive','restricted'
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS policy_profiles_tenant_idx ON public.policy_profiles(tenant_id);

-- ============================================================
-- route_observations: raw/semi-raw per-request telemetry
-- ============================================================
CREATE TABLE IF NOT EXISTS public.route_observations (
    observation_id      bigserial PRIMARY KEY,
    route_id             uuid NOT NULL REFERENCES public.routes(route_id) ON DELETE CASCADE,
    tenant_id            text,
    timestamp            timestamptz NOT NULL,
    latency_ms           integer,
    status_code          integer,
    success               boolean NOT NULL,
    input_tokens          integer,
    output_tokens          integer,
    estimated_cost_usd    numeric(12, 6),
    actual_cost_usd       numeric(12, 6),
    metadata             jsonb
);

CREATE INDEX IF NOT EXISTS route_observations_route_ts_idx
    ON public.route_observations(route_id, timestamp DESC);

-- ============================================================
-- route_intelligence: rolled-up intelligence per route
-- ============================================================
CREATE TABLE IF NOT EXISTS public.route_intelligence (
    route_id                     uuid PRIMARY KEY REFERENCES public.routes(route_id) ON DELETE CASCADE,
    observed_cost_per_1k_tokens  numeric(12, 6),
    latency_p50_ms               integer,
    latency_p95_ms                integer,
    success_rate_5m               numeric(5, 4),
    success_rate_1h               numeric(5, 4),
    success_rate_24h              numeric(5, 4),
    error_rate_5m                 numeric(5, 4),
    availability                  numeric(5, 4),
    confidence                    numeric(5, 4),
    last_updated                  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- preflight_decisions: log of every decision made
-- ============================================================
CREATE TABLE IF NOT EXISTS public.preflight_decisions (
    decision_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             text NOT NULL,
    request_hash          text NOT NULL,     -- normalized request hash, no raw prompt
    mode                  text NOT NULL,     -- 'observe' or 'optimize'
    selected_route_id      uuid,             -- NULL when decision = 'fallback'
    decision_type          text NOT NULL,    -- 'route', 'fallback_existing_route', 'no_route_found'
    candidates             jsonb NOT NULL,   -- [{route_id, status, est_cost, est_latency, rejection_reason?}]
    reason                text NOT NULL,
    decision_latency_ms    numeric(7, 3) NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS preflight_decisions_tenant_created_idx
    ON public.preflight_decisions(tenant_id, created_at DESC);
