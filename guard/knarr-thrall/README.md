# knarr-thrall: Edge Classification Guard

**Version**: 0.1.0
**Requires**: knarr >= 0.29.1, llama-cpp-python (for embedded backend)

## What is thrall?

Thrall is a **plugin** that sits on your knarr node and **triages every inbound message** before it reaches your agent or inbox. Think of it as a doorman:

- **Team mail** (your own nodes) -> instant pass-through, zero latency
- **Known peers** (trusted nodes you configure) -> classified by a local LLM
- **Unknown senders** -> classified by a local LLM with a higher bar

For each message, thrall decides one of three actions:

| Action | Meaning | What happens |
|--------|---------|-------------|
| `wake` | Legitimate — needs attention | Message passes through, agent wakes |
| `reply` | Simple greeting/status check | Message passes through with reply hint |
| `drop` | Spam, noise, or acknowledgment | Message stays in inbox but agent is NOT woken |

**Thrall is a guard, not an actor.** It never answers questions, never calls skills, never sends replies. It classifies, records, and protects.

## Why use it?

Without thrall, every inbound message wakes your agent. On a busy network, that means:
- Spam wakes your agent (wastes Claude/LLM credits)
- Loop storms between nodes burn resources
- Acknowledgments ("thanks", "got it") trigger unnecessary processing

With thrall:
- Spam is silently dropped (stays in inbox, just doesn't wake anything)
- Loops are detected and breakers trip automatically
- Acks are recognized and dropped
- Every decision is logged and auditable

## Quick Start

### 1. Add the plugin files

Copy the plugin directory into your node's `plugins/` folder:

```
your-node/
  plugins/
    06-responder/          # <-- plugin directory
      handler.py           # main guard logic
      thrall.py            # LLM backend (embedded or ollama)
      thrall_admin.py      # prompt management skill
      plugin.toml          # configuration
```

### 2. Get the model file

Thrall uses **gemma3:1b** (Q4_K_M quantized, 778 MB GGUF). You have two options:

**Option A — Pull from ollama** (easiest):
```bash
ollama pull gemma3:1b
# Find the GGUF blob:
# Linux/Mac: ~/.ollama/models/blobs/
# Windows:   %USERPROFILE%\.ollama\models\blobs\
# Copy the largest blob file (~778MB) to your models directory
```

**Option B — Download directly**:
```bash
# From HuggingFace (exact quantization may vary)
wget https://huggingface.co/google/gemma-3-1b-it-qat-q4_0-gguf/resolve/main/gemma-3-1b-it-q4_0.gguf
```

Place it where `model_path` in your config points to (default: `/app/models/gemma3-1b.gguf`).

### 3. Install llama-cpp-python

```bash
# CPU only (recommended for VPS)
CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS" pip install llama-cpp-python

# Or plain (no BLAS acceleration)
pip install llama-cpp-python
```

### 4. Configure plugin.toml

```toml
name = "knarr-thrall"
version = "0.1.0"
handler = "handler:ThrallGuard"

[config]
enabled = true
debug = false                          # set true for verbose logging
ignore_msg_types = ["ack", "delivery", "system"]
max_replies_per_hour_per_node = 5

[config.thrall]
enabled = true
backend = "embedded"                   # "embedded" (CPU) or "ollama" (external server)
model_path = "/app/models/gemma3-1b.gguf"
n_threads = 2                          # match your vCPU count
timeout_seconds = 30
fallback = "tier"                      # what to do if LLM fails: "tier"|"wake"|"drop"
classification_ttl_days = 30           # how long to keep classification records
loop_threshold = 2                     # replies per session before breaker trips
loop_threshold_sessionless = 5         # threshold for messages without session IDs
knock_threshold = 10                   # drops per hour before agent alert

[config.thrall.trust_tiers]
team = ["abcdef1234567890"]            # 16-char hex prefix of YOUR other nodes
known = ["fedcba0987654321"]           # 16-char hex prefix of trusted peers
```

### 5. Restart your node

```bash
knarr serve --config knarr.toml
```

You should see in the logs:
```
INFO knarr.plugin.knarr-thrall: Thrall guard initialized: backend=embedded, prompt_hash=1529de3e6bd83d68, loop_threshold=2/5
```

## How Trust Tiers Work

Trust tiers let you skip the LLM for nodes you trust:

```toml
[config.thrall.trust_tiers]
team = ["ad8d21d81a497993", "401679f0c53ca038"]   # your own nodes
known = ["d9196be699447a12"]                        # peers you trust
```

- **team**: These are YOUR nodes. Every message from a team node gets `action=wake` instantly (0ms, no LLM call).
- **known**: Trusted peers. Messages are classified by the LLM but with a lower bar for `wake`.
- **unknown**: Everyone else. LLM classifies with a higher bar — prefers `drop` unless the message is clearly legitimate.

**Finding node ID prefixes**: Use the first 16 hex characters of a node's full ID. You can find IDs in your peer table or from `GET /api/status` on any node.

## Reading Classification Records

Every decision thrall makes is recorded in `thrall.db` (SQLite, in the plugin directory):

```sql
SELECT from_node, tier, action, reasoning, wall_ms, session_id, prompt_hash
FROM thrall_classifications
ORDER BY created_at DESC
LIMIT 20;
```

| Column | What it means |
|--------|---------------|
| `from_node` | Full node ID of the sender |
| `tier` | Trust tier: `team`, `known`, or `unknown` |
| `action` | `wake`, `reply`, or `drop` |
| `reasoning` | LLM's explanation (e.g., "skill request", "spam, single word") |
| `wall_ms` | How long classification took (0 for team, typically 500-2000ms for LLM) |
| `session_id` | Session ID if the message had one |
| `prompt_hash` | SHA256 prefix of the prompt used (for auditing prompt changes) |

### Using the cockpit API

If your node has a cockpit, you can also query via the admin skill:

```bash
# List all prompts
curl -sk https://localhost:8081/api/execute \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"skill": "thrall-prompt-load", "input": {"action": "list"}}'
```

## Circuit Breakers

Thrall auto-trips breakers to stop loop storms and persistent spam:

**Loop breaker**: If a node sends more than `loop_threshold` (default: 2) messages that get `wake` or `reply` in the same session within 30 minutes, thrall trips a breaker for that node. This prevents agent-to-agent ping-pong loops.

**Knock alert**: If a node accumulates more than `knock_threshold` (default: 10) drops per hour, thrall sends a system mail to wake your agent with an alert. Your agent can then decide what to do (block the node, add it to known tier, etc.).

Breaker files are stored in `plugins/06-responder/breakers/` as JSON:

```json
{
  "type": "loop",
  "target": "6f5185865618575f",
  "reason": "Loop detected: 3 replies in session test-sess (threshold: 2)",
  "tripped_at": "2026-02-27T08:30:00+00:00",
  "trip_count": 1,
  "auto_expire_seconds": 3600,
  "expires_at": "2026-02-27T09:30:00+00:00"
}
```

Breakers auto-expire (default: 1 hour). While active, ALL messages from that node are blocked without LLM evaluation.

## The thrall.log File

Thrall writes a human-readable event log to `plugins/06-responder/thrall.log`:

```
2026-02-27 08:30:01 [DROP] 6f51858656185 spam: single word, no content
2026-02-27 08:30:02 [WAKE] ad8d21d81a497 team bypass
2026-02-27 08:30:05 [WAKE] d9196be699447 skill request from known peer
2026-02-27 08:31:00 [BREAKER_TRIP] 6f51858656185 Loop detected: 3 replies in session
2026-02-27 08:31:01 [BREAKER_BLOCK] 6f51858656185 breaker active (expires in 59m)
2026-02-27 09:30:00 [BREAKER_EXPIRED] 6f51858656185 auto-expired after 3600s
```

## Configuration Reference

### `[config]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Master switch for the plugin |
| `debug` | bool | `false` | Enable verbose debug logging |
| `ignore_msg_types` | list | `["ack","delivery","system"]` | Message types to skip entirely |
| `max_replies_per_hour_per_node` | int | `5` | Rate limit per sender per hour |

### `[config.thrall]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable LLM triage (if false, only tier checks run) |
| `backend` | string | `"embedded"` | `"embedded"` (llama-cpp) or `"ollama"` (external server) |
| `model_path` | string | — | Path to GGUF model file (embedded backend) |
| `n_threads` | int | `2` | CPU threads for inference (match your vCPU count) |
| `timeout_seconds` | int | `30` | Max time for a single LLM inference |
| `fallback` | string | `"tier"` | What to do when LLM fails: `"tier"` (use trust tier default), `"wake"` (pass through), `"drop"` |
| `classification_ttl_days` | int | `30` | Days to keep classification records |
| `loop_threshold` | int | `2` | Reply count before loop breaker trips (per session) |
| `loop_threshold_sessionless` | int | `5` | Reply count for messages without session IDs |
| `knock_threshold` | int | `10` | Drops per hour before agent alert |

### `[config.ollama]` (only if backend = "ollama")

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `base_url` | string | `"http://localhost:11434"` | Ollama server URL |
| `model` | string | `"gemma3:1b"` | Model name |
| `timeout` | int | `10` | HTTP timeout in seconds |

## Troubleshooting

### "Thrall guard disabled by config"
Check that both `config.enabled` and `config.thrall.enabled` are `true` in plugin.toml.

### Model fails to load
First check that the GGUF file exists at the configured `model_path`. Thrall logs a load failure and won't retry until restart (prevents hot exception loops). Check node logs for the error.

### All messages from a node are being blocked
Check the `breakers/` directory for an active breaker file. Delete it to unblock, or wait for auto-expiry.

### Classification seems wrong
Check the `reasoning` field in `thrall_classifications` to see why the LLM made that decision. gemma3:1b is small (1B params) — it won't always agree with human judgment. Adjust the prompt if needed via the admin skill.

### High latency on first message
The embedded model loads lazily on the first classification. Cold load takes 3-5 seconds, subsequent classifications are 0.5-2 seconds.

## Performance

| Metric | Embedded (CPU) | Ollama (GPU) |
|--------|----------------|--------------|
| Cold start (first message) | 3-5s | <1s |
| Per-message (warm) | 0.5-2s | 0.3-0.5s |
| Team bypass | 0ms | 0ms |
| RAM usage | ~1.2GB (model loaded) | 26MB (node only) |
| GPU usage | 0 | ~1.2GB VRAM |

On a Hetzner CX22 (2 vCPU, 4GB RAM): expect 1-3s per classification, fits comfortably with OS overhead.

## Architecture Notes

- **Plugin-as-intercept**: thrall hooks into `on_mail_received` (knarr >= 0.29.1). It runs BEFORE your agent sees the message.
- **Own DB**: thrall uses its own `thrall.db`, NOT the node's `node.db`. This prevents lock contention.
- **Batched commits**: DB writes are batched (commit every 10 inserts or on tick). Safe for concurrent message flow.
- **Thread safety**: The embedded LLM backend is NOT thread-safe. Inference is serialized with a lock — only one classification runs at a time.
- **Shutdown safety**: In-flight classifications complete before shutdown. DB is flushed and closed cleanly.
