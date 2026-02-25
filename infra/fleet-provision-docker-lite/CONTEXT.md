# fleet-provision-docker-lite

Provision knarr nodes as Docker containers on a local bridge network. Part of the Fenrir fleet pipeline.

## What it does

Takes a prepared deployment payload and creates a Docker container with:
- Fixed IP on a dedicated bridge network (`fenrir-fleet`, 172.21.0.0/24)
- Pre-seeded node.db identity injected via `docker cp`
- Generated knarr.toml with bootstrap and provisioner peer override
- Echo skill for proof-of-life
- Memory (256MB) and PID (50) limits for resource isolation

No cloud API needed. Runs entirely on the local Docker daemon.

## Pipeline position

Block skill in a provisioning chain:

```
prepare-payload → setup-docker (this) → wait-for-boot → register-fleet
```

## Input

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| payload_json | yes | — | JSON from prepare-payload (nonce, label, location, node_db_b64, echo_source, etc.) |
| dry_run | no | true | "true" validates without creating, "false" provisions |

## Output

```
status: ok | dry_run | error
ip: container IP on fenrir-fleet network
server_id: container name (fenrir-{label})
provider: "docker"
```

## Requirements

- Docker installed and running
- `knarr-cluster-node:latest` Docker image (build from knarr repo)
- No API key needed (local Docker only)

## Bundled files

```
fleet-provision-docker-lite/
  handler.py                # Skill handler
  cloud_init.py             # Node TOML generator
  fleet_db.py               # Fleet tracking SQLite DB
  providers/
    __init__.py             # ProviderInterface base class
    docker_provider.py      # Docker provider (create, inject, start, delete)
```

## Fleet DB

Tracks all provisioned nodes (label, IP, status, nonce). Auto-creates `fleet.db` in the parent directory on first use. The `get_next_docker_ip()` method allocates IPs from 172.21.0.10-249, skipping nodes that aren't destroyed.

## Registration (knarr.toml)

```toml
[skills.fleet-provision-docker-lite]
handler = "skills/fleet_provision_docker_lite.py:handle"
description = "Provision knarr nodes as Docker containers"
tags = ["infra", "docker", "fleet", "provisioning"]
input_schema = {payload_json = "string", dry_run = "string"}
price = 0
visibility = "private"
```

## Security

- No API keys needed (local Docker daemon)
- Containers resource-limited (256MB RAM, 50 PIDs)
- Dry run by default
- Node identity pre-seeded via base64-encoded node.db (no secrets in transit)
