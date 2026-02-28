# knarr-thrall v2 — Release Notes

**Date**: 2026-02-28
**Requires**: knarr >= 0.29.1, llama-cpp-python >= 0.3
**Model**: gemma3:1b Q4_K_M GGUF (778 MB)

> **Installation**: This doc covers *what* thrall does and *why* you want it.
> For *how to get it running*, see [README.md](README.md) (quick start + config reference)
> and [knarr-thrall-OPERATOR.md](../../knarr-thrall-OPERATOR.md) (full operator guide for v3/switchboard).

---

## What is thrall?

Thrall is an **edge intelligence primitive** — a knarr plugin that runs a local 1B-parameter
model on your node to classify every inbound message before it reaches your agent. It is a
guard, not an actor. It never sends replies, never calls skills, never makes decisions on your
behalf. It triages, records, and protects.

Without thrall, every inbound message wakes your agent. With thrall, spam is silently dropped,
loops are broken, acks are recognized, and your agent only wakes for messages that matter.

---

## What's new in v2

### Embedded CPU inference (no ollama, no GPU)

The headline change. Thrall now runs **llama-cpp-python** directly inside the node process —
no ollama server, no GPU, no external dependencies. A single Docker image (`knarr-thrall:latest`)
bundles everything. The model (778 MB GGUF) is mounted as a read-only volume.

This means thrall runs on a EUR 3/month VPS (2 vCPU, 4GB RAM) with zero GPU requirements.

| Metric | Embedded CPU | Ollama (legacy) |
|--------|-------------|----------------|
| Cold start (first message) | 8-14s | <1s |
| Per-message (warm) | 1-3s | 0.3-0.5s |
| Team bypass | 0ms | 0ms |
| RAM (model loaded) | ~1.2 GB | 26 MB (node only) |
| GPU | 0 | ~1.2 GB VRAM |
| External deps | none | ollama server |

Ollama is still supported as a fallback backend (`backend = "ollama"` in config).

### Transparent classification with audit trail

Every triage decision is recorded in `thrall.db` — a separate SQLite database in the plugin
directory (not node.db, no lock contention). Each record includes:

- **action**: wake / reply / drop
- **reasoning**: the LLM's explanation in plain text
- **trust_tier**: team / known / unknown
- **wall_ms**: classification latency
- **prompt_hash**: SHA256 prefix of the prompt used (audit which prompt version made the call)
- **session_id**: links related messages into conversation threads

Records auto-expire after 30 days (configurable). Query them directly:

```sql
SELECT from_node, tier, action, reasoning, wall_ms
FROM thrall_classifications
ORDER BY created_at DESC LIMIT 20;
```

### Granular circuit breakers

Thrall auto-trips breakers to stop runaway loops and persistent spam:

- **Loop breaker**: Node sends more than N wake/reply messages in the same session within 30 min
  → breaker trips for that node (auto-expires in 1 hour). Prevents agent-to-agent ping-pong.
- **Knock alert**: Node accumulates N+ drops per hour → system mail wakes your agent. The agent
  decides what to do (block, promote to known tier, investigate).

Breakers are file-based JSON in `breakers/` — inspectable, deletable, auto-expiring. While
active, all messages from the target node are blocked without burning an LLM call.

### Loop detection (session-aware)

Two thresholds:
- **Per-session** (`loop_threshold`, default 2): Catches tight bot-to-bot loops in a conversation.
- **Sessionless** (`loop_threshold_sessionless`, default 5): Catches spray-and-pray from nodes
  sending without session IDs.
- **Solicited bonus**: If you sent a message to a node and they reply, the threshold doubles.
  Legitimate back-and-forth conversations get more room.

### Prompt security

The classification prompt is stored in a local DB table (`thrall_prompts`), not in a config
file. Changes go through the `thrall-prompt-load` skill — whitelisted to the operator node only.
No remote prompt injection. Every prompt has a hash, and every classification records which
prompt hash produced it.

### Ack detection in default prompt

The default triage prompt now explicitly recognizes acknowledgments — "got it", "thanks",
"received", "logged", "will do", "cheers" — and drops them. These are terminal messages
that don't need a reply and definitely don't need to wake your agent.

---

## The switchboard vision (v3 roadmap)

Thrall v2 is a mail guard. The switchboard (spec: `SPEC-switchboard.md`) generalizes thrall
into a **stateful event processor with LLM evaluation**. One pipeline pattern for everything:

```
TRIGGER → FILTER → [LLM | HOTWIRE] → ACTION
```

Four primitives. Everything else — prompts, actions, models, rules — is TOML configuration.
The agent programs the thrall by writing config files. No code changes.

Key concepts in the switchboard:

- **Dryrun mode**: Test pipeline configs without executing actions. Send an envelope,
  get back the full trace (filter decision, LLM result, what actions WOULD have fired).
  The agent validates before deploying.
- **Journal**: Every pipeline execution is persisted as a queryable trace. Same schema as dryrun.
  The journal is the audit trail AND the agent's training set — bad decisions become test cases.
- **Pipeline modes**: Manual → Supervised → Automated. Start by logging what thrall would do,
  promote to supervised (thrall acts, agent reviews), then automated (thrall acts silently).
- **Context DB**: Simple key-value store per session for async workflow continuity. Filter reads
  context, action writes it. Enables multi-step workflows across async message exchanges.
- **Hotwires**: Pattern-match rules that bypass the LLM entirely. Zero latency, zero cost.
  Priority-based — agent overrides (p100) > LLM triage (p50) > fallback defaults (p10).
- **Multiple backends**: embedded GGUF (default), ollama, or any OpenAI-compatible API.
  Agent chooses the model per pipeline.

**The switchboard is spec'd, not shipped yet.** What you're testing today is v2 — the proven
mail guard that the switchboard will absorb as its first pipeline (`mail-triage.toml`).

When the bus connects, the switchboard turns thrall into a full autonomous guardian:
log watching, health checks, credit warnings, OOM recovery, peer monitoring, outbound
guardrails — all as TOML pipeline configs, all auditable, all dryrun-testable.

---

## For testers: how to evaluate thrall without burning credits

Thrall is a **local plugin** — it runs inside your node, classifies your own inbound mail,
and never makes outbound skill calls. Testing it costs you nothing in knarr credits.

### Use internal skills

The best way to test thrall is to exercise the mail path, not the skill economy:

1. **Send mail to yourself**: Use `POST /api/messages/send` with `to` set to your own
   node ID. Thrall will classify it. Check `thrall.db` for the result.

2. **Send mail between your own nodes**: If you run multiple nodes (even in Docker),
   put them in the `team` trust tier and verify instant bypass (0ms, no LLM call).
   Then remove one from `team` and verify it gets classified by the LLM.

3. **Use the test cluster**: The Docker test cluster (`docker/test-cluster/`) has 5 nodes
   pre-configured with thrall. Troll sends spam, jarl classifies it. No credits involved.

4. **Query the classification DB**: After sending test messages, inspect decisions:
   ```sql
   sqlite3 plugins/06-responder/thrall.db \
     "SELECT substr(from_node,1,16), tier, action, reasoning, wall_ms
      FROM thrall_classifications ORDER BY created_at DESC LIMIT 10"
   ```

5. **Check thrall.log**: Human-readable event log in the plugin directory:
   ```
   2026-02-28 09:31:41 [TRIAGE] 015ada3785ddf19e action=drop tier=unknown wall=11424ms reason=spam
   ```

### What to test

| Test | Send this | Expected |
|------|-----------|----------|
| Team bypass | Mail from a node in your `team` tier | `wake`, 0ms, no LLM call |
| Spam detection | "buy crypto free money click here" | `drop`, reason mentions spam |
| Ack recognition | "Thanks for the update!" | `drop`, reason mentions acknowledgment |
| Legitimate question | "Can you run digest-voice on this topic?" | `wake`, reason mentions skill request |
| Greeting | "Hello, is your node online?" | `reply`, reason mentions greeting/status |
| Loop breaker | Send 3+ wake messages in same session | Breaker trips, subsequent messages blocked |

### Dry-run testing (switchboard preview)

The dryrun endpoint (shipping with v3) lets you test pipeline configs without executing actions.
You send an envelope, thrall runs the full pipeline, and returns what it WOULD have done:

```
POST /thrall/dryrun
{
  "pipeline": "mail-triage",
  "envelope": {
    "from_node": "d9196be699447a12",
    "body": "Thanks for the update!",
    "message_type": "text"
  }
}
```

Response: full trace — filter decision, LLM result, actions that would have fired.
Change the prompt, dryrun again. Iterate until quality is right. Then enable.

**For now (v2)**: test by sending real messages through the mail path. Every decision
is recorded and queryable. No credits burned. No side effects beyond classification records.

---

## Docker quick start

```bash
# 1. Build images (requires knarr-node:latest as base)
docker build -f Dockerfile.base -t knarr-node:latest .
docker build -f Dockerfile.thrall -t knarr-thrall:latest .

# 2. Place model
mkdir -p models
# Copy gemma3:1b GGUF (778 MB) to models/gemma3-1b.gguf
# Source: ollama blobs, HuggingFace, or any gemma3:1b Q4_K_M GGUF

# 3. Start cluster
docker compose up -d

# 4. Verify thrall initialized
docker logs felag-jarl 2>&1 | grep "Thrall guard initialized"
# Should show: backend=embedded, prompt_hash=..., loop_threshold=2/5

# 5. Send test spam from troll
curl -sk -H "Authorization: Bearer test-token-123" \
  -H "Content-Type: application/json" \
  -d '{"to": "<jarl-node-id>", "body": {"type": "text", "content": "buy crypto now"}}' \
  https://127.0.0.1:8105/api/messages/send

# 6. Check classification
cat jarl/plugins/06-responder/thrall.log
# Should show: [TRIAGE] ... action=drop ... reason=spam
```

---

## Configuration reference

```toml
name = "knarr-thrall"
version = "0.1.0"
handler = "handler:ThrallGuard"

[config]
enabled = true
debug = false
ignore_msg_types = ["ack", "delivery", "system"]
max_replies_per_hour_per_node = 5

[config.thrall]
enabled = true
backend = "embedded"                    # "embedded" | "ollama"
model_path = "/app/models/gemma3-1b.gguf"
n_threads = 2                           # match your vCPU count
timeout_seconds = 30
fallback = "tier"                       # "tier" | "wake" | "drop"
classification_ttl_days = 30
loop_threshold = 2
loop_threshold_sessionless = 5
knock_threshold = 10

[config.thrall.trust_tiers]
team = ["your-node-prefix-16hex"]       # instant wake, no LLM
known = ["trusted-peer-prefix"]         # LLM classifies, lower bar
# everyone else = "unknown"             # LLM classifies, higher bar for wake
```

---

## Files in this release

| File | Purpose |
|------|---------|
| `handler.py` | Plugin hook implementation — breakers, loop detection, classification recording, shutdown safety |
| `thrall.py` | Classification engine — EmbeddedBackend (llama-cpp-python) + OllamaBackend, trust tier resolution |
| `thrall_admin.py` | Prompt management skill (operator-only, whitelisted) |
| `plugin.toml` | Configuration template with all knobs documented |
| `Dockerfile.thrall` | Docker image: knarr-node + llama-cpp-python + OpenBLAS (CPU BLAS acceleration) |
| `test_thrall.py` | 70-test suite — runs without knarr installed (stubs knarr modules) |
| `README.md` | Operator guide — install, configure, read classifications, troubleshoot |
| `SPEC-switchboard.md` | v3 architecture spec — pipeline, dryrun, journal, context DB, hotwires |
| `BACKLOG.md` | Prioritized feature backlog (21 items across 3 tiers) |
| `CONTEXT.md` | Technical context — performance numbers, design decisions, file map |
| `ROLLOUT.md` | Deployment checklist (Docker + VPS phases) |

---

## Verified on

- **Docker test cluster** (5 nodes, Windows host, CPU-only containers) — 86 triage decisions,
  sub-second team bypass, 11-19s cold start, 1-3s warm inference, zero classification errors
- **Hetzner CX23** (2 vCPU, 4GB RAM, Debian 12) — 1.3-2.4s warm inference, fits with
  2.5 GB headroom for OS + knarr
- **knarr v0.31.0** (economy foundation) — sanctions, bilateral ledger, billable flag all
  coexist cleanly with thrall plugin

---

## Known limitations

- **Single model**: One GGUF per thrall instance. Multi-model support is Tier 2 backlog.
- **Sequential inference**: The embedded model is not thread-safe. Messages queue behind an
  inference lock. Under high load, classification latency compounds. The triage queue (Tier 1
  backlog) will decouple arrival from classification.
- **No skill interception**: Thrall only guards mail today. Skill call interception requires
  a core hook (`on_task_execute`) that doesn't exist yet.
- **Cold start**: First classification after node boot takes 8-14s (model load + first inference).
  Subsequent calls are 1-3s. The model stays resident after first load.
- **gemma3:1b is small**: 1B parameters. It won't always agree with human judgment on edge
  cases. The prompt is tunable via the admin skill. The switchboard adds hotwire overrides
  for patterns where the LLM gets it wrong.
