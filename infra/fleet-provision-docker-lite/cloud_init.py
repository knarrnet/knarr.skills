"""Node TOML generation for Docker-based knarr nodes."""

import logging

logger = logging.getLogger("fenrir.cloud_init")


def generate_node_toml(
    label: str,
    port: int,
    sidecar_port: int,
    cockpit_port: int,
    provisioner_node_id: str,
    provisioner_host: str,
    provisioner_port: int,
) -> str:
    """Generate knarr.toml for a Docker container node."""
    prov_prefix = provisioner_node_id[:16]
    return f"""[node]
host = "0.0.0.0"
port = {port}
sidecar_port = {sidecar_port}
storage = "node.db"
jurisdiction = "eu"

[cockpit]
port = {cockpit_port}

[network]
bootstrap = ["bootstrap1.knarr.network:9000", "bootstrap2.knarr.network:9000"]
upnp = false

[peer_overrides]
{prov_prefix} = "{provisioner_host}:{provisioner_port}"

[policy]
initial_credit = 100000
min_balance = -100000

[skills.echo]
description = "Echo skill — proof of life for {label}"
handler = "skills/echo.py:handle"
price = 0
tags = ["echo", "fenrir"]
"""
