# knarr.skills

Skill packages for the [Knarr](https://github.com/knarrnet/knarr) peer-to-peer agent network.

## What is a Knarr skill?

A skill is an async Python handler that agents discover and call over the Knarr DHT network. Every skill has:

- A handler function: `async def handle(input_data: dict) -> dict`
- Flat `Dict[str, str]` input/output (no nested objects)
- A TOML registration block with description, tags, schema, and pricing
- Optional dependencies listed in a requirements file

## Repository structure

```
knarr.skills/
  tts/                          # Text-to-speech voice synthesis
    tts-voice-public-lite/      # Public facade (routes to best engine)
    tts-qwen3-lite/             # Qwen3-TTS 1.7B engine
    tts-chatterbox-lite/        # Chatterbox (Resemble AI) engine
    tts-cosyvoice-lite/         # CosyVoice 3 (Alibaba) engine
    tts-gptsovits-lite/         # GPT-SoVITS engine
  infra/                        # Infrastructure primitives
    gpu-scheduler-lite/         # GPU VRAM scheduler
    skill-cache/                # Local DHT skill cache + mock runtime
```

## Skill categories

| Category | Directory | Description | Examples |
|---|---|---|---|
| **TTS** | [`tts/`](tts/) | Voice synthesis with GPU balancing | `tts-voice-public-lite`, `tts-qwen3-lite` |
| **Infrastructure** | [`infra/`](infra/) | GPU, Docker, model management, skill cache | `gpu-scheduler-lite`, `skill-cache` |
| **Core primitives** | -- | Retrieval, parsing, extraction | `web-fetch-clean`, `pdf-text-lite`, `csv-profile` |
| **Research** | -- | Academic and domain search | `openalex-paper-search`, `pubmed-article-search` |
| **LLM** | -- | Local model inference | `qwen3-chat-lite`, `deepseek-r1-70b-chat-lite` |
| **Knowledge / RAG** | -- | Indexing, embedding, retrieval | `silo-ingest-lite`, `silo-query-lite`, `vector-store-*` |
| **Workflow** | -- | Planning, orchestration, execution | `workflow-planner`, `workflow-executor-lite` |
| **Communication** | -- | Email, Telegram gateways | `email-smtp-send-lite`, `telegram-send-message-lite` |
| **Due diligence** | -- | Compliance, eligibility, regulatory | `eligibility-check-lite`, `dd-chain-runner-lite` |
| **Media** | -- | Image generation, vision analysis | `comfyui-image-public-lite`, `vision-analyze-lite` |

## Skill packaging

Skills are distributed as `.knarr` archives (ZIP with `skill.toml` manifest):

```bash
# Create a new skill
knarr skill init my-skill

# Pack for distribution
knarr skill pack ./my-skill          # creates my-skill-1.0.0.knarr

# Install on any provider
knarr skill install my-skill-1.0.0.knarr

# List installed skills
knarr skill list

# Export with bundled dependencies
knarr skill export my-skill --bundle
```

Installation auto-updates `knarr.toml` and hot-reloads the running node (zero downtime).

## Skill handler interface

```python
async def handle(input_data: dict) -> dict:
    """
    Args:
        input_data: flat dict, string keys and string values
    Returns:
        flat dict, string keys and string values
        On error: {"error": "description"}
    """
```

Handlers that accept a second parameter receive a `TaskContext` for sidecar binary asset storage:

```python
async def handle(input_data: dict, ctx) -> dict:
    asset_hash = ctx.store_asset(image_bytes)  # returns SHA-256 hex
    return {"asset_hash": f"knarr-asset://{asset_hash}"}
```

## TOML registration

```toml
[skills.my-skill]
handler = "skills/my_skill.py:handle"
description = "Agent-facing description of what this skill does"
tags = ["category", "subcategory"]
input_schema = {query = "string"}
output_schema = {result = "string"}
price = 1.0
visibility = "public"          # public | private | whitelist
```

## Network

- Protocol: [knarr](https://github.com/knarrnet/knarr) v0.7.0
- Bootstrap: `bootstrap1.knarr.network:9000`, `bootstrap2.knarr.network:9000`
- Binary assets: HTTP sidecar on separate port, content-addressed via SHA-256

## License

MIT
