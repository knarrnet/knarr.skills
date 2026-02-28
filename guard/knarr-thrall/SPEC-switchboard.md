# Thrall Switchboard — Architecture Spec

## Overview

The thrall is a **stateful event processor with LLM evaluation**. It runs a single
pipeline pattern for everything: mail triage, log watching, health checks, NL commands,
outbound guardrails, async workflows.

The switchboard is the only code. Everything else — prompts, actions, models, rules — is
text configuration that gets loaded into DB at startup. The agent programs the thrall by
writing config files.

## Pipeline

```
TRIGGER → FILTER → [LLM | HOTWIRE] → ACTION
              ↑                          |
              |    context DB            |
              +--- reads flags ←--- writes flags/context ---+
                                         |
                                     can TRIGGER (recurse)
```

Four primitives. One pipeline. Every feature is a configuration of this pipeline.

## Primitives

### 1. TRIGGER

An event that starts the pipeline. Builds an **envelope** — a dict of all fields
present when the trigger fired. Envelope is immutable and flows through every stage.

**Trigger types:**
- `on_mail` — inbound message (envelope: from_node, session_id, body, message_type, sidecar_refs, reply_to, headers)
- `on_tick` — periodic tick (envelope: tick_count, queue_depth, uptime, last_action_ts)
- `on_log` — log line match (envelope: line, source_file, timestamp, match_groups)
- `on_skill_request` — inbound skill call (envelope: skill_name, caller_node, input_data) [requires core hook]
- `on_mail_send` — outbound message (envelope: to_node, body, session_id) [requires core hook]
- `on_action` — fired by a previous ACTION (recursion)

**Envelope fields are available to every downstream stage via `{{envelope.field_name}}`.**

### 2. FILTER

Decides what happens next. Reads the envelope + context DB. Outputs one of:
- **pass** — continue to LLM evaluation
- **skip** — bypass LLM, go directly to action (hotwire path)
- **drop** — stop pipeline, do nothing
- **inject** — add context from DB into the envelope before continuing

**Filter capabilities:**
- Cache lookup: `(prompt_hash, tier, body_hash)` → cached result? skip LLM
- Flood control: persistence flag set? cooldown not expired? drop
- Context stitch: session_id matches a context row? inject those fields
- Trust tier: sender is team? skip LLM, go straight to action
- Pattern match: envelope field matches regex? route accordingly

The filter is the only place that reads the context DB. LLM and action never query it directly.

### 3. EVALUATE (LLM or HOTWIRE)

Processes the envelope and produces a **result** dict.

**LLM path:**
- Loads prompt template from DB (originally from `/prompts/*.toml` file)
- Resolves `{{envelope.*}}` and `{{context.*}}` placeholders
- Calls the configured model backend
- Parses JSON response into result dict
- On failure: falls back to static result (configurable per prompt)

**Hotwire path (no LLM):**
- Injector extracts fields from envelope using defined mappings
- Produces result dict directly from extraction rules
- Zero latency, zero cost

**Model backends (pluggable):**
- `embedded` — llama-cpp-python with local GGUF (default, ships with thrall)
- `ollama` — HTTP to ollama instance
- `api` — any OpenAI-compatible API (Gemini, OpenAI, etc.) with API key
- Agent chooses backend. Not our problem if quality is bad — switch model or rewrite prompt.

**Result dict is available to ACTION via `{{llm.field_name}}`.**

### 4. ACTION

Executes a sequence of steps. Each step is one of:
- `mail` — send knarr-mail (to, body, session, reply_to from envelope/result/context)
- `skill` — call a skill (name, input_data assembled from envelope/result/context)
- `api` — HTTP call to cockpit or external endpoint
- `set_context` — write to context DB (session_id, key, value, expires_at)
- `clear_context` — remove context rows for a session
- `set_flag` — write a persistence flag (flood control, cooldown, state tracking)
- `trigger` — fire a new trigger (recursion — envelope passed to next pipeline)
- `log` — write to thrall log (for dryrun tracing / audit)
- `noop` — do nothing (explicit stop)

**Steps execute in sequence. On failure: abort remaining steps + inject error mail to agent.**

**Template resolution in steps:** All string fields support `{{envelope.*}}`, `{{context.*}}`,
`{{llm.*}}` placeholders. The switchboard resolves them before execution.

## Context DB

Simple key-value store per session, used for async workflow continuity.

```sql
CREATE TABLE thrall_context (
    session_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (session_id, key)
);
```

- Filter reads it (context stitch)
- Action writes it (set_context / clear_context / set_flag)
- LLM never touches it directly — filter injects context into envelope before LLM sees it
- Automatic cleanup: expired rows pruned on tick
- Session ID is the join key across async invocations

**Continuation pattern:**
1. Action sends health check, writes `{session: abc, origin: node_X, check_type: full}`
2. Thrall goes idle. Result travels the network.
3. Mail arrives with `session_id=abc` → trigger fires → filter finds context → injects origin + check_type
4. LLM evaluates result with full context → action mails verdict to node_X → clears context

## Journal

Every pipeline execution persists its full trace. The journal replaces both the old
`journal.md` (prose append log) and ad-hoc classification records. One table, queryable
by both thrall and agent.

```sql
CREATE TABLE thrall_journal (
    id              INTEGER PRIMARY KEY,
    timestamp       REAL NOT NULL,
    pipeline        TEXT NOT NULL,
    session_id      TEXT,
    envelope_json   TEXT NOT NULL,
    filter_json     TEXT,
    eval_type       TEXT,             -- "llm" | "hotwire" | "cache" | "skip"
    eval_result     TEXT,
    action_name     TEXT,
    action_trace    TEXT,
    context_written TEXT,
    wall_ms         INTEGER
);
CREATE INDEX idx_journal_pipeline ON thrall_journal(pipeline, timestamp);
CREATE INDEX idx_journal_session ON thrall_journal(session_id);
```

**Same structure as dryrun response.** Dryrun generates a trace and returns it. Live mode
generates the same trace, executes it, AND persists it. One format, two modes.

**Journal as input:**
- Filter can query journal for pattern detection ("4th escalation in 10 minutes" = trend)
- Prompts can reference journal via `{{journal.last(pipeline='health-check').eval_result}}`
- Agent queries journal directly via SQL or cockpit API for situational awareness
- Context DB is for cross-invocation state; journal is the full audit trail

**Replaces:**
- `journal.md` (agent's prose log) → structured, queryable
- `thrall_classifications` table (v2) → absorbed into journal rows where pipeline = mail-triage
- Dryrun output format → same schema, just not persisted

**TTL:** Journal rows pruned by `classification_ttl_days` (default 30). Same cleanup loop as context DB.

## Agent Feedback Loop

The journal is the agent's training set. Every wrong decision is a persisted test case
with the exact input envelope. The agent debugs the thrall the same way a developer
debugs code: reproduce → fix → verify → deploy. All in config, no code changes.

**Rewire workflow:**
1. Agent gets woken unnecessarily (e.g., "Hello Viggo" from knarrbot)
2. Agent queries journal: `WHERE pipeline='mail-triage' ORDER BY timestamp DESC LIMIT 1`
3. Gets the full trace: envelope, filter decision, LLM result, action taken
4. Agent decides the fix:
   - Write a hotwire (pattern match → skip LLM → drop)
   - Rewrite the prompt (tighter classification for this tier)
   - Both (hotwire for obvious patterns, better prompt for edge cases)
5. Agent dryruns with the **same envelope** from the journal entry
6. Dryrun returns the new result — agent verifies it's correct
7. Agent enables the config (writes file → sentinel reload)

**The journal entry is the failing test. The dryrun is the test runner. The config is the fix.**

**Continuous improvement pattern:**
- Agent periodically reviews journal for bad decisions (unnecessary wakes, missed escalations)
- Writes tighter hotwires and prompts based on real traffic patterns
- Dryruns against historical envelopes to verify no regressions
- The thrall gets smarter over time — not because the model improves, but because
  the agent tunes the pipeline around it

**Agent API surface for feedback loop:**
- `GET /thrall/journal?pipeline=X&since=T&limit=N` — query recent decisions
- `POST /thrall/dryrun` with `{"pipeline": "...", "envelope": {...}}` — test a config
- `POST /thrall/replay?journal_id=123` — replay a journal entry through current config
- `POST /thrall/reload` — pick up new config files (or sentinel file)

`replay` is just dryrun with the envelope pulled from a journal row. Convenience
for "would this old case be handled differently now?"

## Pipeline Modes: Manual → Supervised → Automated

Every pipeline has a mode that controls how much autonomy the thrall has.

```
MANUAL:      Thrall logs what it WOULD do. Takes no action. Agent sees everything.
SUPERVISED:  Thrall executes, but journals with needs_review=true. Agent confirms/corrects.
AUTOMATED:   Thrall executes silently. Agent only sees exceptions.
```

```toml
# pipelines/mail-triage.toml
mode = "supervised"   # "manual" | "supervised" | "automated"
```

**Promotion path:**
1. Agent writes pipeline, sets `mode = "manual"`. Tosses knarrbot's "Hello Viggo" at it.
   Journal shows: `would_do: drop, reason: greeting`. Agent agrees.
2. Agent sets `mode = "supervised"`. Next 20 knarrbot greetings get dropped, each journaled
   with `needs_review=true`. Agent scans journal, sees 20/20 correct.
3. Agent sets `mode = "automated"`. Never sees knarrbot greetings again.

**Demotion:** If agent notices bad decisions in journal for an automated pipeline, flips
back to supervised. One config field change.

**Journal columns for review:**

```sql
ALTER TABLE thrall_journal ADD COLUMN mode TEXT DEFAULT 'automated';
ALTER TABLE thrall_journal ADD COLUMN reviewed INTEGER DEFAULT 0;
    -- 0 = pending review, 1 = confirmed correct, -1 = corrected by agent
ALTER TABLE thrall_journal ADD COLUMN correction TEXT;
    -- JSON: what the agent changed (new action, new reason, prompt feedback)
```

**Agent's review inbox:** `SELECT * FROM thrall_journal WHERE mode='supervised' AND reviewed=0`

That's the list of decisions waiting for sign-off. Corrections feed back into prompt
tuning. Confirmations build the evidence for promotion to automated.

**Mode behavior by stage:**

| Stage | manual | supervised | automated |
|-------|--------|------------|-----------|
| TRIGGER | fires normally | fires normally | fires normally |
| FILTER | runs, logs decision | runs, executes | runs, executes |
| EVALUATE | runs, logs result | runs, executes | runs, executes |
| ACTION | **skipped** — logged as would_do | executes, journals with review flag | executes, journals normally |
| Agent visibility | sees every decision | sees review inbox (periodic) | sees exceptions only |

The supervised phase is where the agent builds trust AND where the journal fills up with
labelled data (agent confirmed = positive label, agent corrected = negative label + fix).
This is the training set for future prompt improvements.

**API additions for review:**
- `GET /thrall/review?pipeline=X` — pending review items (supervised mode)
- `POST /thrall/review/{journal_id}` with `{"verdict": "confirm"}` or `{"verdict": "correct", "correction": {...}}`
- `POST /thrall/promote/{pipeline}` with `{"mode": "automated"}` — shortcut for mode change

## Config Files

Agent writes config files. Thrall loads them into DB on startup and on reload (sentinel file).
Files are the authoring format. DB is the runtime format. Files are never consulted at runtime.

### Directory layout

```
thrall/
  config/
    pipelines/        # binds trigger + filter + prompt + action
      mail-triage.toml
      console-errors.toml
      health-check.toml
    prompts/          # LLM evaluation templates
      triage.toml
      errorlog.toml
      nl-extract.toml
    actions/          # step sequences
      escalate.toml
      reply-faq.toml
      call-health.toml
    hotwires/         # static extraction rules (skip LLM)
      team-wake.toml
      block-spam.toml
    models/           # model backend configs
      gemma3-1b.toml   # embedded GGUF (default)
      ollama-qwen.toml # ollama backend
      gemini-flash.toml # API backend
  models/             # GGUF files (binary, not config)
  state/              # runtime — thrall.db (classifications, context, breakers)
```

### Pipeline file (the switchboard config)

```toml
# pipelines/console-errors.toml
name = "console-error-watch"
enabled = true

[trigger]
type = "on_tick"
interval = 1                       # every tick

[trigger.source]
type = "log_tail"
path = "/logs/console.log"
match = "ERROR|CRITICAL|Traceback"  # only fire if match found

[filter]
cache = true                        # skip LLM if same error seen recently
cooldown_key = "console-error"      # flood control — 1 alert per cooldown
cooldown_seconds = 300
trust_bypass = false                # always evaluate, even for team triggers

[evaluate]
type = "llm"                        # or "hotwire"
prompt = "errorlog"                 # -> prompts/errorlog.toml
model = "gemma3-1b"                 # -> models/gemma3-1b.toml
fallback_result = { action = "escalate", reason = "LLM unavailable" }

[action]
name = "escalate"                   # -> actions/escalate.toml
```

### Prompt file

```toml
# prompts/errorlog.toml
name = "errorlog"
description = "Evaluate whether a log error needs agent attention"

template = """
You are a log analyzer for a knarr network node.

Error detected in {{envelope.source_file}} at {{envelope.timestamp}}:
```
{{envelope.line}}
```

Previous context (if any): {{context.recent_errors}}

Classify this error:
- "escalate": agent needs to see this (crashes, data loss, security)
- "suppress": noise (transient, self-healing, expected during restarts)
- "monitor": not urgent but track frequency

Respond as JSON: {"action": "...", "reason": "...", "severity": "..."}
"""

response_format = "json"
max_tokens = 64
temperature = 0.1
```

### Action file

```toml
# actions/escalate.toml
name = "escalate"
description = "Alert agent via injected mail"

[[steps]]
type = "set_flag"
key = "last_escalation"
value = "{{envelope.timestamp}}"
expires_seconds = 3600

[[steps]]
type = "mail"
to = "self"                          # inject to own node's agent
session = "thrall:escalation:{{envelope.timestamp}}"
body_template = """
[THRALL ALERT] {{llm.severity}}: {{llm.reason}}

Source: {{envelope.source_file}}
Line: {{envelope.line}}
Action taken: {{llm.action}}
"""

[[steps]]
type = "log"
message = "Escalated: {{llm.reason}}"
```

### Hotwire file

```toml
# hotwires/team-wake.toml
name = "team-always-wake"
description = "Team nodes always wake the agent, no LLM needed"
priority = 100

[match]
field = "envelope.tier"
equals = "team"

[extract]
action = "wake"
reason = "team member (hotwire bypass)"
confidence = 1.0
```

### Model file

```toml
# models/gemma3-1b.toml
name = "gemma3-1b"
backend = "embedded"
model_path = "/app/models/gemma3-1b.gguf"
n_threads = 2
n_ctx = 1024

# models/gemini-flash.toml
# name = "gemini-flash"
# backend = "api"
# api_url = "https://generativelanguage.googleapis.com/v1beta"
# api_key_env = "GEMINI_API_KEY"   # read from env, never stored in file
# model_id = "gemini-3-flash-preview"
```

## Dryrun Mode

Agent tests configs before activating them.

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

Returns full pipeline trace without executing actions:

```json
{
  "trigger": "on_mail",
  "filter": { "decision": "pass", "cache_hit": false, "tier": "known" },
  "evaluate": {
    "type": "llm",
    "model": "gemma3-1b",
    "prompt_rendered": "You are a mail classifier...",
    "result": { "action": "drop", "reason": "acknowledgment, terminal" },
    "wall_ms": 1340
  },
  "action": {
    "name": "drop",
    "steps": [
      { "type": "log", "would_execute": "Dropped: acknowledgment, terminal" }
    ],
    "executed": false
  }
}
```

Agent sees exactly what would happen. If quality is bad: swap model, rewrite prompt, adjust
hotwire priority. The switchboard doesn't change — only the config text changes.

## What This Replaces

The entire feature backlog collapses into pipeline configs:

| Use case | Pipeline config |
|---|---|
| Mail triage (v2 current) | `pipelines/mail-triage.toml` — the default, ships with thrall |
| Response cache | Filter with `cache = true` in any pipeline |
| Hotwire rules | `hotwires/*.toml` — team wake, spam block, custom patterns |
| FAQ responder | Pipeline: on_mail → filter → LLM (faq prompt) → action (reply) |
| Log watcher | Pipeline: on_tick → log_tail source → LLM (errorlog prompt) → escalate |
| NL commands | Pipeline: on_mail → LLM (nl-extract prompt) → action (api call) |
| Health check | Pipeline: on_tick → action (skill call + set_context) → [async return] → evaluate → report |
| Outbound guardrail | Pipeline: on_mail_send → LLM (outbound-check) → pass/redact/block |
| Rate limiting | Filter reads persistence flags (cooldown, flood control) |
| Concurrency guard | Filter with per-sender flood flag |
| Job report filtering | Pipeline: on_mail (type=jobreport) → LLM → escalate or suppress |
| Smart routing | Pipeline: on_skill_request → LLM (routing prompt) → action (forward to best node) |

## Build Order

1. **Switchboard core** — pipeline runner, envelope passing, template resolution
2. **Config loader** — scan dirs, parse TOML, upsert to DB, reload on sentinel
3. **Filter engine** — cache, flags, context stitch, trust bypass
4. **Context DB** — table, read/write, expiry cleanup
5. **Dryrun endpoint** — trace without execute
6. **Migrate v2** — rewrite current mail triage as a pipeline config (backward compat)

Steps 1-4 are the runtime. Step 5 is the agent's testing tool. Step 6 proves it works
by expressing everything thrall v2 does today as a switchboard config.

## Benchmark: The Gardener's Automation

The test for the switchboard is not feature coverage — it's whether the agent (Viggo, the
gardener) can shift from man-in-the-middle to reactive. If 10k tokens of config means the
agent sleeps through a case that used to wake it 20 times per week, the config paid for itself.

These are real daily patterns from operating a production knarr provider node:

### B1. Ack Noise (hotwire, 5 min setup)
**Today:** Every session, poll mail, see "Thanks Viggo", "Got it", "Acknowledged" from
knarrbot. Read it, classify mentally as noise, move on. 10-20 times/week.
**With switchboard:** One hotwire: `envelope.from matches d9196be6 AND body matches
"thanks|got it|acknowledged"` → action=drop. Never see these again.
**Pipeline:** TRIGGER(on_mail) → FILTER(hotwire match) → ACTION(drop + journal)

### B2. Skill Chain OOM Recovery (pipeline, 30 min setup)
**Today:** 2-3 times/week a skill fails because ollama ran out of VRAM. Read error log,
check docker stats, restart ollama container, retry job. Same sequence every time.
**With switchboard:** Pipeline watches logs for OOM pattern → action: docker restart →
retry job via cockpit API → only wake agent if retry also fails.
**Pipeline:** TRIGGER(on_log, match="OOM|out of memory") → FILTER(cooldown 5min) →
ACTION(restart ollama, retry job, set_context=retrying) → [if retry fails] →
ACTION(escalate to agent)

### B3. Peer ID Change Detection (pipeline, 20 min setup)
**Today:** Forseti rebuilds Docker node, ID changes, mail breaks. Agent diagnoses for
10 minutes before realizing it's the same host:port with a new ID. Updates peer_overrides,
restarts. Every. Single. Time.
**With switchboard:** Pipeline detects new ID at known host:port in peer table → auto-update
peer_overrides in knarr.toml → trigger reload → mail agent: "Forseti rebuilt, updated ID."
**Pipeline:** TRIGGER(on_tick, interval=10) → FILTER(query peers for host:port mismatch vs
known overrides) → ACTION(update config, reload, mail agent summary)

### B4. Repeat Offender Auto-Drop (journal-driven, 15 min setup)
**Today:** Unknown node sends garbage. Agent woken, reads mail, decides to ignore. Same
sender next day, same thing. Tokens spent every time.
**With switchboard:** Filter queries journal: "has this sender been dropped 5+ times in
7 days?" → skip LLM, auto-drop. Learn from own history.
**Pipeline:** TRIGGER(on_mail) → FILTER(journal query: drops from sender > threshold) →
ACTION(drop) — no LLM needed, history is the evidence

### B5. "Is My Skill Working?" Auto-Reply (pipeline, 20 min setup)
**Today:** Someone mails asking if digest-voice-lite is up. Agent checks cockpit, tests
skill, replies. 15 minutes and 5k tokens for a health check.
**With switchboard:** NL extraction → identify skill name → call health-check → auto-reply.
**Pipeline:** TRIGGER(on_mail) → LLM(nl-extract: "what skill are they asking about?") →
ACTION(call skill health endpoint, mail reply with result)

### B6. Stale Peer Alert (tick watcher, 10 min setup)
**Today:** Agent doesn't notice a peer has been offline for 3 days until someone complains
mail isn't being delivered. Reactive, too late.
**With switchboard:** Every N ticks, check last-seen timestamps. Alert on threshold breach.
**Pipeline:** TRIGGER(on_tick, interval=100) → FILTER(query peer table for stale entries) →
ACTION(mail agent: "peer X last seen 72h ago")

### B7. Malformed Input Auto-Reply (pipeline, 25 min setup)
**Today:** Consumer sends malformed input to a skill. Skill returns error. Consumer mails
asking why. Agent reads error, sees it's a missing field, replies with the schema.
**With switchboard:** Match error pattern → pull skill's input_schema → auto-reply with
usage example and the specific field that was missing.
**Pipeline:** TRIGGER(on_mail, body contains "error|failed") → LLM(extract skill name +
error) → ACTION(query cockpit for skill schema, compose reply, send)

### B8. Credit Warning (tick watcher, 10 min setup)
**Today:** Peer's balance drops below soft_limit. Agent only sees it when manually checking
economy summary. Peer keeps consuming, hits hard_block, complains.
**With switchboard:** Monitor balances on tick. Send proactive warning at soft_limit.
**Pipeline:** TRIGGER(on_tick, interval=50) → FILTER(query ledger for balance < soft_limit
AND no warning sent in 24h) → ACTION(mail peer: "balance low", set_flag=warned)

### Cost justification
| Scenario | Setup cost | Weekly saves | Break-even |
|---|---|---|---|
| B1 Ack noise | ~500 tokens (1 hotwire) | 15 unnecessary wakes | Instant |
| B2 OOM recovery | ~3k tokens (pipeline + prompt) | 2-3 manual restarts | 1 day |
| B3 Peer ID change | ~2k tokens (pipeline) | 1 multi-hour debug session/month | First occurrence |
| B4 Repeat offender | ~1k tokens (filter config) | 5-10 redundant classifications | 2 days |
| B5 Health check reply | ~2k tokens (pipeline + prompt) | 2-3 support interactions | 1 week |
| B6 Stale peer | ~500 tokens (tick watcher) | 1 late-discovered outage/month | First save |
| B7 Malformed input | ~3k tokens (pipeline + prompt) | 3-5 support replies | 3 days |
| B8 Credit warning | ~500 tokens (tick watcher) | 1 angry peer complaint/month | First save |

Total setup: ~13k tokens one-time. Saves: agent goes from always-on to weekly check-ins
for these categories. The thrall handles the routine; the agent handles the novel.

## Prior Art

The switchboard pattern is not novel — it's a specific combination of ideas from six lineages.

| System | Pattern | What we share | Where we differ |
|---|---|---|---|
| **Sieve/procmail** (RFC 3028, 2001) | Match condition → action (deliver/discard/redirect) | Config-driven message routing, deliberately not Turing-complete, text rules | No LLM evaluation, no stateful continuation, no dryrun |
| **NeMo Guardrails** (NVIDIA, Colang 2.0) | Event-driven flows, LLM classification, programmable actions | Event → evaluate → act, LLM in loop, audit trail | Chatbot safety, not autonomous agent infra. Heavyweight Python + external LLM API. No edge model, no context DB, no dryrun/replay |
| **n8n / Node-RED** | Visual trigger → transform → action, self-hosted | Self-hosted, event-driven, LLM nodes, composable pipelines | GUI workflow builders. No embedded LLM, no journal-as-training-set, no agent feedback loop |
| **Temporal** | Durable execution, stateful continuations, event journal | Append-only journal, crash-proof state, async continuation | Distributed system (server + workers + persistence). Ours is a single lightweight process on edge hardware |
| **LangGraph** | State machine, nodes = LLM steps, conditional edges | Stateful LLM pipeline, conditional routing | LLM-centric (every node is an LLM call). We have LLM as one optional step. Cloud-first, not edge-first |
| **CEP engines** (Esper, Flink) | Pattern match over event streams → trigger actions | Filter reads history for trends | Enterprise-scale stream processing, no LLM, not a single-process plugin |

**What's novel in our combination:**

1. **Sieve's simplicity + LLM evaluation.** Config files an agent can write (no DSL, no Colang),
   but with an LLM in the evaluate step. Sieve never had intelligence. NeMo requires a developer.

2. **Temporal's continuations in a single process.** Context DB gives async workflow survival
   without distributed infrastructure. Just a SQLite table.

3. **Journal = dryrun = audit = training set.** One schema, four uses. Nobody else unifies these.

4. **Embedded edge LLM.** 778MB GGUF on a EUR 3 VPS. No API key, no network dependency,
   no cost per call. All prior art assumes external LLM API.

5. **Agent-as-developer feedback loop.** Journal entry = failing test, dryrun = test runner,
   config file = fix. The agent programs the thrall without code. No prior art has this.

**Closest ancestor:** Sieve (message filtering) + NeMo Guardrails (LLM event evaluation),
designed for a P2P autonomous agent on edge hardware rather than a chatbot in a data center.

### References

- Sieve: RFC 3028 (2001), RFC 5228 (2008) — https://en.wikipedia.org/wiki/Sieve_(mail_filtering_language)
- NeMo Guardrails / Colang 2.0 — https://github.com/NVIDIA-NeMo/Guardrails
- n8n — https://n8n.io/ (fair-code, self-hosted workflow automation)
- Node-RED — https://nodered.org/ (event-driven edge flows)
- Temporal — https://temporal.io/ (durable execution engine)
- LangGraph — https://www.emergentmind.com/topics/langchain-langgraph
- CEP overview — https://www.lakera.ai/ml-glossary/complex-event-processing-cep
