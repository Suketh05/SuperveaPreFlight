import pytest

from app.preflight.persistence.queries import load_policy
from app.preflight.schemas.api_models import PreflightConstraints


class FakeDB:
    """In-memory stand-in for asyncpg.Connection — no real Postgres needed."""

    def __init__(self, row: dict | None):
        self.row = row
        self.fetchrow_calls = 0

    async def fetchrow(self, sql, policy_profile_id):
        self.fetchrow_calls += 1
        return self.row


class FakeRedisCache:
    """In-memory stand-in for RedisCache — no real Redis needed."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get_json(self, key):
        self.get_calls += 1
        return self.store.get(key)

    async def set_json(self, key, value, ttl_seconds=None):
        self.set_calls += 1
        self.store[key] = value


def make_row(**overrides) -> dict:
    base = {
        "allowed_providers": ["openai"],
        "allowed_models": None,
        "allowed_regions": ["US"],
        "max_cost_usd": 1.0,
        "max_latency_ms": 2000,
        "required_capabilities": None,
        "data_classification": "internal",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_cache_miss_populates_cache_after_db_read():
    db = FakeDB(row=make_row())
    redis_cache = FakeRedisCache()

    result = await load_policy(db, "policy-1", redis_cache)

    assert db.fetchrow_calls == 1
    assert isinstance(result, PreflightConstraints)
    assert result.allowed_providers == ["openai"]
    assert redis_cache.store["policy:profile:policy-1"] == result.model_dump()


@pytest.mark.asyncio
async def test_second_call_hits_cache_not_db():
    db = FakeDB(row=make_row())
    redis_cache = FakeRedisCache()

    first = await load_policy(db, "policy-1", redis_cache)
    second = await load_policy(db, "policy-1", redis_cache)

    assert db.fetchrow_calls == 1
    assert second == first


@pytest.mark.asyncio
async def test_no_redis_cache_falls_through_to_db_every_time():
    db = FakeDB(row=make_row())

    await load_policy(db, "policy-1", None)
    await load_policy(db, "policy-1", None)

    assert db.fetchrow_calls == 2


@pytest.mark.asyncio
async def test_empty_policy_profile_id_short_circuits_without_db_or_cache():
    db = FakeDB(row=make_row())
    redis_cache = FakeRedisCache()

    result = await load_policy(db, None, redis_cache)

    assert db.fetchrow_calls == 0
    assert redis_cache.get_calls == 0
    assert redis_cache.set_calls == 0
    assert result == PreflightConstraints()
