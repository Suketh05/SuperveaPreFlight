"""Standard interface every gateway/router adapter must implement.

Adapters are replaceable plug-ins, not hard-coded dependencies. If an
adapter fails, that only degrades route freshness for its routes — it
never touches or breaks live customer traffic (data plane).
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.preflight.schemas.internal_models import DiscoveredRoute


class GatewayAdapter(ABC):
    def __init__(self, gateway_config: dict):
        self.config = gateway_config

    @abstractmethod
    def discover_routes(self) -> AsyncIterator[DiscoveredRoute]:
        """Yield every route this gateway currently exposes."""
        ...

    @abstractmethod
    async def get_health(self) -> dict:
        ...

    @abstractmethod
    async def get_usage(self) -> dict:
        ...

    @abstractmethod
    async def get_pricing(self) -> dict:
        ...

    @abstractmethod
    async def get_metadata(self) -> dict:
        ...

    async def test_route(self, route_id: str) -> dict | None:
        """Optional: adapters may implement a lightweight route probe."""
        return None
