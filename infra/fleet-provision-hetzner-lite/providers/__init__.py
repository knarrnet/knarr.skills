"""Provider abstraction for VPS provisioning."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProvisionResult:
    ip: str
    server_id: str
    provider: str
    location: str
    label: str


class ProviderInterface(ABC):
    """Abstract base for VPS providers."""

    @abstractmethod
    async def create_server(self, label: str, location: str, user_data: str) -> ProvisionResult:
        """Create a server with cloud-init user_data. Returns ProvisionResult."""
        ...

    @abstractmethod
    async def delete_server(self, server_id: str) -> bool:
        """Delete a server by ID. Returns True on success."""
        ...

    @abstractmethod
    async def server_status(self, server_id: str) -> dict:
        """Get server status. Returns dict with at least 'status' key."""
        ...
