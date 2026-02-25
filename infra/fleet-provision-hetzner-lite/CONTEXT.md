# fleet-provision-hetzner-lite

Provision knarr nodes on Hetzner Cloud VPS. Part of the Fenrir fleet pipeline.

## What it does

Takes a prepared deployment payload and creates a Hetzner Cloud server with:
- Ubuntu 24.04 base image
- Cloud-init script that installs Python, knarr, and starts the node
- Pre-seeded node.db identity (node ID known before server boots)
- SSH keys attached from your Hetzner account
- Systemd service for automatic restart

The provisioned node joins the DHT within ~60 seconds of boot.

## Pipeline position

This is a **block skill** in a provisioning chain:

```
prepare-payload → setup-hetzner (this) → wait-for-boot → register-fleet
```

The `payload_json` input comes from a prepare-payload step that generates the node identity, encryption keys, and echo skill source.

## Input

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| payload_json | yes | — | JSON from prepare-payload (nonce, label, location, node_db_b64, etc.) |
| hetzner_api_token | yes (live) | — | Hetzner Cloud API token (injected via knarr secrets) |
| dry_run | no | true | "true" validates without creating, "false" provisions |

## Output

```
status: ok | dry_run | error
ip: public IPv4 address
server_id: Hetzner server ID (numeric)
provider: "hetzner"
```

## Requirements

- `hcloud` Python package (Hetzner Cloud SDK)
- Hetzner API token with server create/delete permissions
- Token injected via knarr secret injection (never hardcoded)

## Bundled files

```
fleet-provision-hetzner-lite/
  handler.py           # Skill handler
  cloud_init.py        # Cloud-init script generator
  providers/
    __init__.py        # ProviderInterface base class
    hetzner.py         # Hetzner Cloud provider
```

## Registration (knarr.toml)

```toml
[skills.fleet-provision-hetzner-lite]
handler = "skills/fleet_provision_hetzner_lite.py:handle"
description = "Provision knarr nodes on Hetzner Cloud VPS"
tags = ["infra", "hetzner", "fleet", "provisioning"]
input_schema = {payload_json = "string", dry_run = "string"}
price = 0
visibility = "private"

[secrets.fleet-provision-hetzner-lite]
hetzner_api_token = "vault:hetzner_api_token"
```

## Security

- API token injected at runtime via `input_data`, never stored in code
- Dry run mode by default — must explicitly set `dry_run = "false"` to provision
- SSH keys fetched from Hetzner account, not stored locally
