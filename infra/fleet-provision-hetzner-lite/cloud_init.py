"""Cloud-init script generation for knarr node deployment on VPS."""

import logging

logger = logging.getLogger("fenrir.cloud_init")

KNARR_GIT_URL = "https://github.com/knarrnet/knarr.git"


def generate_cloud_init(
    nonce: str,
    provisioner_node_id: str,
    provisioner_host: str,
    provisioner_port: int,
    label: str,
    location: str,
    node_db_b64: str,
    knarr_version: str,
    echo_skill_source: str,
    port: int = 9010,
    sidecar_port: int = 9011,
    cockpit_port: int = 8080,
) -> str:
    """Generate cloud-init bash script for a new knarr node.

    The script:
    1. Installs Python + venv
    2. Creates /opt/knarr with venv and pip install knarr
    3. Writes pre-seeded node.db from base64
    4. Writes knarr.toml
    5. Writes echo skill handler
    6. Creates systemd service
    7. Starts the node
    """
    import base64
    echo_b64 = base64.b64encode(echo_skill_source.encode()).decode()

    knarr_toml = generate_node_toml(label, port, sidecar_port, cockpit_port, provisioner_node_id, provisioner_host, provisioner_port)
    toml_b64 = base64.b64encode(knarr_toml.encode()).decode()

    script = f"""#!/bin/bash
set -euo pipefail
exec > /var/log/fenrir-bootstrap.log 2>&1
echo "=== Fenrir Bootstrap Start: $(date -u) ==="
echo "Label: {label} | Location: {location} | Nonce: {nonce}"

# System packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git

# Create knarr directory
mkdir -p /opt/knarr/skills
cd /opt/knarr

# Python venv
python3 -m venv venv
source venv/bin/activate

# Install knarr from git
pip install --quiet "git+{KNARR_GIT_URL}@{knarr_version}"

# Write pre-seeded node.db
echo "{node_db_b64}" | base64 -d > /opt/knarr/node.db
echo "Pre-seeded node.db written ($(wc -c < /opt/knarr/node.db) bytes)"

# Write knarr.toml
echo "{toml_b64}" | base64 -d > /opt/knarr/knarr.toml
echo "knarr.toml written"

# Write echo skill
echo "{echo_b64}" | base64 -d > /opt/knarr/skills/echo.py
echo "Echo skill written"

# Create systemd service
cat > /etc/systemd/system/knarr.service << 'SYSTEMD'
[Unit]
Description=Knarr Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/knarr
ExecStart=/opt/knarr/venv/bin/python -m knarr serve --config knarr.toml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable knarr.service
systemctl start knarr.service

echo "=== Fenrir Bootstrap Complete: $(date -u) ==="
echo "Knarr service started. Node should appear on DHT within 60s."
"""
    return script


def generate_node_toml(
    label: str,
    port: int,
    sidecar_port: int,
    cockpit_port: int,
    provisioner_node_id: str,
    provisioner_host: str,
    provisioner_port: int,
) -> str:
    """Generate knarr.toml for a VPS node."""
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
