"""fleet-provision-hetzner-lite — Create a Hetzner VPS with cloud-init for knarr.

Part of the Fenrir fleet provisioning pipeline. Takes a prepared payload
(from a prepare-payload step) and provisions a Hetzner Cloud VPS with
a pre-seeded knarr node identity.

Input:
  - payload_json: JSON from prepare-payload step (contains nonce, label, location,
    provisioner info, node_db_b64, knarr_version, echo_source)
  - hetzner_api_token: Hetzner Cloud API token (injected via knarr secrets)
  - dry_run: "true" or "false" (default: true)

Output:
  - status: ok | dry_run | error
  - ip: server public IPv4
  - server_id: Hetzner server ID
  - provider: "hetzner"
"""

import json
import logging
import os
import sys

# Use bundled provider libs
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers.hetzner import HetznerProvider
from cloud_init import generate_cloud_init

logger = logging.getLogger("fenrir.setup_hetzner")

NODE = None


def set_node(node):
    global NODE
    NODE = node


async def handle(input_data: dict) -> dict:
    payload_json = input_data.get("payload_json", "")
    dry_run = input_data.get("dry_run", "true").strip().lower() == "true"
    api_token = input_data.get("hetzner_api_token", "")

    if not payload_json:
        return {"status": "error", "error": "payload_json is required"}

    if not api_token and not dry_run:
        return {"status": "error", "error": "hetzner_api_token required for live deploy"}

    payload = json.loads(payload_json)

    cloud_init_script = generate_cloud_init(
        nonce=payload["nonce"],
        provisioner_node_id=payload["provisioner_node_id"],
        provisioner_host=payload["provisioner_host"],
        provisioner_port=payload["provisioner_port"],
        label=payload["label"],
        location=payload["location"],
        node_db_b64=payload["node_db_b64"],
        knarr_version=payload["knarr_version"],
        echo_skill_source=payload["echo_source"],
    )

    provider = HetznerProvider(api_token or "dry-run-token", dry_run=dry_run)

    ssh_keys = []
    if not dry_run:
        try:
            ssh_keys = await provider.get_ssh_key_objects()
            logger.info(f"Attaching {len(ssh_keys)} SSH keys")
        except Exception as e:
            logger.warning(f"Failed to fetch SSH keys: {e}")

    result = await provider.create_server(
        payload["label"], payload["location"], cloud_init_script, ssh_keys=ssh_keys,
    )

    return {
        "status": "dry_run" if dry_run else "ok",
        "ip": result.ip,
        "server_id": result.server_id,
        "provider": result.provider,
    }
