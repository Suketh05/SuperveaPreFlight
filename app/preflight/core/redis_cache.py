"""Environment-prefixed Redis cache wrapper.

All keys are prefixed with "<env>:" to avoid cross-environment bleed
(e.g. staging data being read as prod data). This is a deliberate
Annexure-A mitigation, not an afterthought.
"""

import json
from typing import Any, Optional

from redis.asyncio import Redis


class RedisCache:
    def __init__(self, client: Redis, env: str = "prod"):
        self.client = client
        self.env = env

    def _key(self, key: str) -> str:
        return f"{self.env}:{key}"

    async def get_json(self, key: str) -> Optional[dict]:
        raw = await self.client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def get_json_many(self, keys: list[str]) -> dict[str, Optional[dict]]:
        """Fetches several keys in ONE round trip via MGET instead of one
        GET per key. Keyed by the UNPREFIXED keys passed in — callers
        shouldn't need to know about env-prefixing.
        """
        if not keys:
            return {}

        prefixed = [self._key(k) for k in keys]
        raw_values = await self.client.mget(prefixed)
        return {
            key: (json.loads(raw) if raw is not None else None)
            for key, raw in zip(keys, raw_values)
        }

    async def set_json(self, key: str, value: dict, ttl_seconds: Optional[int] = None) -> None:
        raw = json.dumps(value, separators=(",", ":"), default=str)
        if ttl_seconds:
            await self.client.set(self._key(key), raw, ex=ttl_seconds)
        else:
            await self.client.set(self._key(key), raw)

    async def delete(self, key: str) -> None:
        await self.client.delete(self._key(key))
