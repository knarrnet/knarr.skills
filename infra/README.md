# Infrastructure Skills

Low-level primitives for GPU management, Docker container lifecycle, and model operations. These are always private -- called internally by higher-level skills, never exposed to the network.

## Skills

| Skill | Description |
|---|---|
| `gpu-scheduler-lite` | Probe GPU VRAM via pynvml, allocate GPUs with automatic load balancing, evict idle containers |
| `skill-cache` | Local SQLite cache of all DHT skills: harvest, search, mock execution for fast chain iteration |

## GPU Scheduler

The GPU scheduler is the foundation for all GPU-accelerated skills (TTS, image generation, LLM inference). It:

1. Probes all GPUs via pynvml for real-time VRAM usage
2. Picks the GPU with the most free memory
3. Returns a device ID that downstream skills pass to Docker
4. Can evict idle containers to free VRAM when needed

### Actions

- `status` -- Probe all GPUs, list running containers and their VRAM usage
- `request` -- Request a GPU with at least N MB free VRAM
- `evict` -- Force-kill containers to free GPU memory

## Skill Cache

The skill cache is the foundation for the Forge -- an autonomous skill chain compiler. It harvests all skill metadata from the DHT into a fast local SQLite database, provides instant querying for chain design, and mock execution so chains can be tested in sub-second time without live GPU calls.

### Sub-skills

| Skill | Visibility | Description |
|---|---|---|
| `skill-cache-query-lite` | public | Search skills by name, tag, text, schema keys, price |
| `skill-cache-init-lite` | private | Create/migrate SQLite DB |
| `skill-cache-harvest-lite` | private | Walk DHT + local skills, upsert into cache |
| `skill-cache-mock-lite` | private | Mock execution: replay > synthetic > live passthrough |
| `skill-cache-stats-lite` | private | Cache health, coverage, harvest history |

### Install

```bash
knarr skill install infra/skill-cache
```

Zero external dependencies -- stdlib only (sqlite3, json, os).
