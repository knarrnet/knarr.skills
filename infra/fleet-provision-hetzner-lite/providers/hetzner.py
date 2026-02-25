"""Hetzner Cloud provider implementation."""

import asyncio
import logging
from functools import partial

from hcloud import Client
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType
from hcloud.servers import Server
from hcloud.ssh_keys import SSHKey

from . import ProviderInterface, ProvisionResult

logger = logging.getLogger("fenrir.hetzner")

# CX23: 2 vCPU, 4GB RAM, 40GB disk — EUR 3.23/mo (CX22 deprecated)
DEFAULT_SERVER_TYPE = "cx23"
DEFAULT_IMAGE = "ubuntu-24.04"

SSH_KEY_NAME = "fenrir-fleet"


class HetznerProvider(ProviderInterface):
    def __init__(self, api_token: str, dry_run: bool = False):
        self._token = api_token
        self._dry_run = dry_run
        self._client = Client(token=api_token)

    async def list_ssh_keys(self) -> list:
        """List all SSH keys on the Hetzner account."""
        loop = asyncio.get_event_loop()
        keys = await loop.run_in_executor(None, self._client.ssh_keys.get_all)
        return [{"id": k.id, "name": k.name, "fingerprint": k.fingerprint} for k in keys]

    async def get_ssh_key_objects(self) -> list:
        """Get all SSH key objects for passing to create_server."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._client.ssh_keys.get_all)

    async def ensure_ssh_key(self, public_key_path: str) -> SSHKey:
        """Register SSH key with Hetzner if not already present. Returns the SSHKey."""
        loop = asyncio.get_event_loop()
        existing = await loop.run_in_executor(
            None, partial(self._client.ssh_keys.get_by_name, SSH_KEY_NAME),
        )
        if existing:
            logger.info(f"SSH key '{SSH_KEY_NAME}' already registered (id={existing.id})")
            return existing

        with open(public_key_path) as f:
            pubkey = f.read().strip()
        result = await loop.run_in_executor(
            None, partial(self._client.ssh_keys.create, name=SSH_KEY_NAME, public_key=pubkey),
        )
        logger.info(f"SSH key '{SSH_KEY_NAME}' registered (id={result.id})")
        return result

    async def create_server(self, label: str, location: str, user_data: str,
                            ssh_keys: list = None) -> ProvisionResult:
        if self._dry_run:
            logger.info(f"[DRY RUN] Would create server: label={label}, location={location}, "
                        f"type={DEFAULT_SERVER_TYPE}, image={DEFAULT_IMAGE}")
            return ProvisionResult(
                ip="0.0.0.0",
                server_id="dry-run-0",
                provider="hetzner",
                location=location,
                label=label,
            )

        loop = asyncio.get_event_loop()
        create_kwargs = dict(
            name=label,
            server_type=ServerType(name=DEFAULT_SERVER_TYPE),
            image=Image(name=DEFAULT_IMAGE),
            location=Location(name=location),
        )
        if user_data:
            create_kwargs["user_data"] = user_data
        if ssh_keys:
            create_kwargs["ssh_keys"] = ssh_keys
        response = await loop.run_in_executor(
            None,
            partial(self._client.servers.create, **create_kwargs),
        )
        server: Server = response.server
        ip = server.public_net.ipv4.ip if server.public_net.ipv4 else "unknown"
        logger.info(f"Created server: id={server.id}, ip={ip}, label={label}")
        return ProvisionResult(
            ip=ip,
            server_id=str(server.id),
            provider="hetzner",
            location=location,
            label=label,
        )

    async def delete_server(self, server_id: str) -> bool:
        if self._dry_run:
            logger.info(f"[DRY RUN] Would delete server: {server_id}")
            return True

        loop = asyncio.get_event_loop()
        try:
            server = await loop.run_in_executor(
                None,
                partial(self._client.servers.get_by_id, int(server_id)),
            )
            await loop.run_in_executor(None, partial(self._client.servers.delete, server))
            logger.info(f"Deleted server: {server_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete server {server_id}: {e}")
            return False

    async def server_status(self, server_id: str) -> dict:
        if self._dry_run:
            return {"status": "dry-run", "server_id": server_id}

        loop = asyncio.get_event_loop()
        try:
            server = await loop.run_in_executor(
                None,
                partial(self._client.servers.get_by_id, int(server_id)),
            )
            return {
                "status": str(server.status),
                "server_id": str(server.id),
                "ip": server.public_net.ipv4.ip if server.public_net.ipv4 else "unknown",
                "name": server.name,
                "location": server.datacenter.location.name if server.datacenter else "unknown",
            }
        except Exception as e:
            logger.error(f"Failed to get server status {server_id}: {e}")
            return {"status": "error", "error": str(e)}
