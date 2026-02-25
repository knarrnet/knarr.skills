# deploy-knarr-lite

Deploy and manage knarr nodes as Docker containers. One skill call = one running node.

## What it does

Creates a Docker container running a knarr node with:
- Protocol port, cockpit, and sidecar exposed
- Volume-mounted config and data (identity persists across rebuilds)
- knarr-mail enabled, cockpit accessible
- Auto-generated cockpit token returned in output

Also supports: status check, stop, remove, upgrade (build new image + redeploy).

## Input

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| name | yes | — | Container name (becomes `knarr-{name}`) |
| action | no | deploy | `deploy`, `status`, `stop`, `remove`, `upgrade` |
| port | no | 9030 | Knarr protocol port |
| cockpit_port | no | 8085 | Cockpit API port |
| sidecar_port | no | port+1 | Sidecar (asset storage) port |
| bootstrap | no | bootstrap1.knarr.network:9000 | Bootstrap peer address |
| advertise_host | yes (deploy) | — | Your LAN or public IP |
| version | yes (upgrade) | — | Git tag (e.g. `v0.29.1`) |

## Output

```
status: ok | error
node_id: 64-char hex node ID
cockpit_token: auto-generated Bearer token
cockpit_url: http://{advertise_host}:{cockpit_port}
container_id: Docker container short ID
container_name: knarr-{name}
```

## Requirements

- Docker installed and running
- `knarr-node:latest` image (or use `upgrade` action to build from git tag)
- Network access to bootstrap peers

## Registration (knarr.toml)

```toml
[skills.deploy-knarr-lite]
handler = "skills/deploy_knarr_lite.py:handle"
description = "Deploy and manage knarr nodes in Docker containers"
tags = ["infra", "docker", "deployment"]
input_schema = {name = "string", action = "string"}
price = 0
visibility = "private"
```

## Notes

- Identity (node.db) persists in host volume — safe to rebuild/upgrade
- Windows: uses MSYS_NO_PATHCONV for git bash compatibility
- Upgrade action builds image from knarr git tag, stops old container, redeploys with same config
