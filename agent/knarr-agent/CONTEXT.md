# knarr-agent

Node-resident autonomous agent plugin for knarr. Install it, give it a model, it runs your node.

## What it does

Sits inside your knarr node. Listens for mail, task failures, peer changes. Routes incoming requests to your skills. Replies to peers. Logs observations. No human in the loop.

The agent is a **router**, not a thinker. A 1.5B parameter model on CPU classifies intent and dispatches to the right skill. The skills do the work. Every dispatched request flows credits through the protocol.

## Install

1. Copy this directory into your node's `plugins/` folder as `05-agent/`
2. Download a GGUF model (default: Qwen 2.5 1.5B Instruct Q4_K_M, ~1GB)
3. Set `model_path` in `plugin.toml` to point at the GGUF file
4. Restart your node

No API key. No GPU. No Docker. Runs on CPU.

```
plugins/
  05-agent/
    plugin.toml      # config — edit this
    prompts/          # agent behavior — edit these
      identity.md
      router.md
      skills.md
      actions.md
      rules.md
    handler.py        # plugin lifecycle
    llm.py            # LLM backends (llama_cpp, ollama, gemini, static)
    actions.py        # action executor (send_mail, call_skill, store_note, log)
    events.py         # event queue and filtering
    memory.py         # SQLite persistence (conversations, notes, rate limits)
    prompts.py        # prompt assembly from markdown files
    scheduler.py      # scheduled jobs (task_stats, daily_digest)
```

## Customize behavior

Edit the markdown files in `prompts/`. They are concatenated into the system prompt at runtime. Variables: `{node_id}`, `{peer_count}`, `{skill_inventory}`.

- `identity.md` — who the agent is
- `router.md` — routing logic and decision flow
- `skills.md` — auto-filled skill inventory (or write your own)
- `actions.md` — available action schemas
- `rules.md` — behavioral constraints

Drop additional `.md` files in the directory — they get picked up automatically, sorted by filename.

No code changes needed. Edit markdown, agent changes behavior next tick.

## LLM backends

| Backend | Config key | Needs | Use case |
|---------|-----------|-------|----------|
| `llama_cpp` | `[config.llama_cpp]` | GGUF file on disk | Default. CPU, no server, no key |
| `ollama` | `[config.ollama]` | Ollama running | Bigger models, GPU inference |
| `gemini` | `[config.gemini]` | API key in vault | Cloud inference, no local compute |
| `static` | — | Nothing | Testing. Logs everything, decides nothing |

## Actions

The agent responds to events with JSON actions:

| Action | What happens |
|--------|-------------|
| `call_skill` | Invokes a local skill, sends result back to sender via mail |
| `send_mail` | Replies directly to a peer |
| `store_note` | Persists a key-value observation in agent.db |
| `log` | Records to provider log |
| `ignore` | Drops the event |

`call_skill` has its own allowlist (`allowed_skills` in plugin.toml) and rate limit.

## Events the agent reacts to

| Event | Source | Config |
|-------|--------|--------|
| Incoming mail | MailSync intercept + mail_inbox poll | `[config.events.mail_received]` |
| Task failure | execution_log poll | `[config.events.task_completed]` |
| Peer join/leave | peer diff on tick | `[config.events.peer_change]` |

## Reporting issues

Problems with the plugin interface, bugs, or change requests: send via knarr-mail to the architect node. The agent can do this itself — just tell it.

## Requirements

- Python 3.12+
- `llama-cpp-python` (for llama_cpp backend)
- knarr v0.29.0+
- A GGUF model file (recommended: Qwen 2.5 1.5B Instruct Q4_K_M)
