import json

import pytest

from app.preflight.core.redis_cache import RedisCache


class FakeRedisClient:
    """In-memory stand-in for redis.asyncio.Redis — only implements mget."""

    def __init__(self, store: dict[str, str]):
        self.store = store
        self.mget_calls = 0

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.store.get(k) for k in keys]


@pytest.mark.asyncio
async def test_get_json_many_batches_into_one_mget_call():
    client = FakeRedisClient(
        {
            "prod:route:pricing:a": json.dumps({"input_price_per_1m": 1.0}),
            "prod:route:pricing:b": json.dumps({"input_price_per_1m": 2.0}),
        }
    )
    cache = RedisCache(client, env="prod")

    result = await cache.get_json_many(
        ["route:pricing:a", "route:pricing:b", "route:pricing:missing"]
    )

    assert result == {
        "route:pricing:a": {"input_price_per_1m": 1.0},
        "route:pricing:b": {"input_price_per_1m": 2.0},
        "route:pricing:missing": None,
    }
    assert client.mget_calls == 1


@pytest.mark.asyncio
async def test_get_json_many_empty_input_makes_zero_calls():
    client = FakeRedisClient({})
    cache = RedisCache(client, env="prod")

    result = await cache.get_json_many([])

    assert result == {}
    assert client.mget_calls == 0
