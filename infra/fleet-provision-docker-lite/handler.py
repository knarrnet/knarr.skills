"""fleet-provision-docker-lite — Create Docker containers with injected knarr identity.

Part of the Fenrir fleet provisioning pipeline. Takes a prepared payload
and provisions a Docker container on a bridge network with pre-seeded
node identity and configuration.

Input:
  - payload_json: JSON from prepare-payload step
  - dry_run: "true" or "false" (default: true)

Output:
  - status: ok | dry_run | error
  - ip: container IP on fleet network
  - server_id: container name (fenrir-{label})
  - provider: "docker"
"""

import base64
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers.docker_provider import DockerProvider
from cloud_init import generate_node_toml
from fleet_db import FleetDB

logger = logging.getLogger("fenrir.setup_docker")

NODE = None
FLEET_DB = None

# Docker containers reach the provisioner via the bridge gateway
DOCKER_GATEWAY = "172.21.0.1"
FENRIR_PORT = 9040


def set_node(node):
    global NODE, FLEET_DB
    NODE = node
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet.db")
    FLEET_DB = FleetDB(db_path)


async def handle(input_data: dict) -> dict:
    payload_json = input_data.get("payload_json", "")
    dry_run = input_data.get("dry_run", "true").strip().lower() == "true"

    if not payload_json:
        return {"status": "error", "error": "payload_json is required"}

    payload = json.loads(payload_json)
    label = payload["label"]

    # Get next free IP
    ip = FLEET_DB.get_next_docker_ip()
    logger.info(f"Allocated Docker IP: {ip}")

    # Regenerate toml for Docker context
    provisioner_node_id = payload["provisioner_node_id"]
    node_toml = generate_node_toml(
        label=label,
        port=9010,
        sidecar_port=9011,
        cockpit_port=8080,
        provisioner_node_id=provisioner_node_id,
        provisioner_host=DOCKER_GATEWAY,
        provisioner_port=FENRIR_PORT,
    )
    node_toml = node_toml.replace(
        'host = "0.0.0.0"',
        f'host = "0.0.0.0"\nadvertise_host = "{ip}"',
    )

    provider = DockerProvider(dry_run=dry_run)

    result = await provider.create_server(label, payload["location"], "", ip=ip)

    if dry_run:
        return {
            "status": "dry_run",
            "ip": ip,
            "server_id": result.server_id,
            "provider": "docker",
        }

    # Write temp files for injection
    tmp_dir = tempfile.mkdtemp(prefix="fenrir-docker-")
    try:
        node_db_path = os.path.join(tmp_dir, "node.db")
        with open(node_db_path, "wb") as f:
            f.write(base64.b64decode(payload["node_db_b64"]))

        toml_path = os.path.join(tmp_dir, "knarr.toml")
        with open(toml_path, "w") as f:
            f.write(node_toml)

        echo_path = os.path.join(tmp_dir, "echo.py")
        with open(echo_path, "w") as f:
            f.write(payload["echo_source"])

        container = result.server_id
        await provider.inject_files(container, {
            "/opt/knarr/node.db": node_db_path,
            "/opt/knarr/knarr.toml": toml_path,
            "/opt/knarr/skills/echo.py": echo_path,
        })

        await provider.start_container(container)
        logger.info(f"Docker container {container} started at {ip}")

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "status": "ok",
        "ip": ip,
        "server_id": result.server_id,
        "provider": "docker",
    }
