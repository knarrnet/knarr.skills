"""Docker provider implementation for fleet provisioning."""

import asyncio
import json
import logging
import subprocess
from functools import partial

from . import ProviderInterface, ProvisionResult

logger = logging.getLogger("fenrir.docker_provider")

DOCKER_IMAGE = "knarr-cluster-node:latest"
NETWORK_NAME = "fenrir-fleet"
NETWORK_SUBNET = "172.21.0.0/24"
NETWORK_GATEWAY = "172.21.0.1"

MEMORY_LIMIT = "256m"
PIDS_LIMIT = 50


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker command with list args (no shell=True)."""
    return subprocess.run(
        args, capture_output=True, text=True, check=check, timeout=60,
    )


class DockerProvider(ProviderInterface):
    def __init__(self, dry_run: bool = False):
        self._dry_run = dry_run

    def ensure_network(self) -> None:
        """Create fenrir-fleet bridge network if it doesn't exist."""
        result = _run(["docker", "network", "ls", "--format", "{{.Name}}"], check=False)
        if NETWORK_NAME in result.stdout.splitlines():
            logger.info(f"Network '{NETWORK_NAME}' already exists")
            return

        _run([
            "docker", "network", "create",
            "--driver", "bridge",
            "--subnet", NETWORK_SUBNET,
            "--gateway", NETWORK_GATEWAY,
            NETWORK_NAME,
        ])
        logger.info(f"Created network '{NETWORK_NAME}' ({NETWORK_SUBNET})")

    async def create_server(self, label: str, location: str, user_data: str,
                            ip: str = None, **kwargs) -> ProvisionResult:
        """Create a Docker container with fixed IP."""
        container_name = f"fenrir-{label}"

        if self._dry_run:
            logger.info(f"[DRY RUN] Would create container: {container_name}, ip={ip}")
            return ProvisionResult(
                ip=ip or "0.0.0.0",
                server_id=container_name,
                provider="docker",
                location=location,
                label=label,
            )

        self.ensure_network()

        loop = asyncio.get_event_loop()
        create_args = [
            "docker", "create",
            "--name", container_name,
            "--network", NETWORK_NAME,
            "--memory", MEMORY_LIMIT,
            "--pids-limit", str(PIDS_LIMIT),
            "--restart", "unless-stopped",
        ]
        if ip:
            create_args.extend(["--ip", ip])

        create_args.append(DOCKER_IMAGE)

        result = await loop.run_in_executor(None, partial(_run, create_args))
        container_id = result.stdout.strip()[:12]
        logger.info(f"Created container {container_name} ({container_id}), ip={ip}")

        return ProvisionResult(
            ip=ip or "unknown",
            server_id=container_name,
            provider="docker",
            location=location,
            label=label,
        )

    async def inject_files(self, container: str, files: dict[str, str]) -> None:
        """Inject files into a stopped container via docker cp."""
        loop = asyncio.get_event_loop()
        for dest, src in files.items():
            await loop.run_in_executor(
                None, partial(_run, ["docker", "cp", src, f"{container}:{dest}"])
            )
            logger.info(f"Injected {src} -> {container}:{dest}")

    async def start_container(self, container: str) -> None:
        """Start a stopped container."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, partial(_run, ["docker", "start", container])
        )
        logger.info(f"Started container {container}")

    async def delete_server(self, server_id: str) -> bool:
        """Remove a container (force)."""
        if self._dry_run:
            logger.info(f"[DRY RUN] Would delete container: {server_id}")
            return True

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, partial(_run, ["docker", "rm", "-f", server_id], check=False)
            )
            logger.info(f"Deleted container {server_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete container {server_id}: {e}")
            return False

    async def server_status(self, server_id: str) -> dict:
        """Get container status via docker inspect."""
        if self._dry_run:
            return {"status": "dry-run", "server_id": server_id}

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                partial(_run, [
                    "docker", "inspect",
                    "--format", "{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}",
                    server_id,
                ], check=False),
            )
            if result.returncode != 0:
                return {"status": "not_found", "server_id": server_id}

            parts = result.stdout.strip().split("|")
            return {
                "status": parts[0] if parts else "unknown",
                "running": parts[1] if len(parts) > 1 else "unknown",
                "started_at": parts[2] if len(parts) > 2 else "unknown",
                "server_id": server_id,
                "provider": "docker",
            }
        except Exception as e:
            logger.error(f"Failed to inspect container {server_id}: {e}")
            return {"status": "error", "error": str(e)}
