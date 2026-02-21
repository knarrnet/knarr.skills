# Knarr Skill SDK

The base class for building skills that don't break.

**The boilerplate IS the standard.** Not "follow these 12 rules and we'll certify you" but "use this base class and you're compliant by default." L1 compliance becomes a pip install, not a process.

## What you get for free

| Feature | What it does | Why it matters |
|---------|-------------|----------------|
| **Healthcheck** | Pings chain dependencies, warms models | Consumer never sees a broken skill |
| **Structured logging** | Tagged, parseable, feeds audit trail | L3 audit compliance by default |
| **Error reporting** | Structured error codes, not stack traces | Consumers get actionable errors, not tracebacks |
| **Input validation** | Schema check before `run()` executes | Bad input rejected at the gate |
| **Execution timing** | Wall time measurement, feeds reputation | Network can rank providers by performance |

## What you write

One function:

```python
from skill_base import SkillBase

class MySkill(SkillBase):
    name = "my-skill-lite"
    required_fields = ["query"]

    async def run(self, data):
        query = data["query"]
        result = do_something(query)
        return {"result": result}
```

Everything else is inherited.

## Quick start

### Leaf skill (simplest)

A skill that does one thing. No dependencies on other skills.

```python
from skill_base import SkillBase

class TranslateSkill(SkillBase):
    name = "translate-lite"
    required_fields = ["text", "target_language"]

    async def run(self, data):
        translated = await translate(data["text"], data["target_language"])
        return {"translated": translated, "source_language": "auto"}

# Module exports — required for serve_batch1.py node injection
_skill = TranslateSkill()

def set_node(node):
    _skill.set_node(node)

async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
```

### Leaf skill with healthcheck

When your skill depends on an external service (Ollama, an API, a database), override `healthcheck()` to verify it's reachable. The healthcheck runs when another skill pings you with `_healthcheck: true`.

```python
import requests
from skill_base import SkillBase

OLLAMA_BASE = "http://localhost:11434"

class CodeReviewSkill(SkillBase):
    name = "code-review-deep-lite"
    required_fields = ["code"]

    async def healthcheck(self):
        """Verify Ollama is reachable. Raise if not."""
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()

    async def run(self, data):
        # Your review logic here
        return {"findings_json": "[]", "risk_level": "clean"}

_skill = CodeReviewSkill()

def set_node(node):
    _skill.set_node(node)

async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
```

### Chain skill (orchestrator)

A skill that calls other skills. Set `chain` to declare your dependencies. The base class automatically pings every link when a healthcheck arrives.

```python
from skill_base import SkillBase

class AuditSkill(SkillBase):
    name = "code-audit-lite"
    chain = ["code-review-deep-lite", "code-vuln-scan-lite"]
    call_local_timeout = 600_000  # 10 min for GPU model loading

    async def run(self, data):
        # Pre-exec healthcheck: verify chain is alive before committing GPU time
        chain_err = await self.check_chain()
        if chain_err:
            return {"error": f"Chain unhealthy: {chain_err}"}

        # Call sub-skills
        review = await self.call("code-review-deep-lite", {"code": data["code"]})
        vuln = await self.call("code-vuln-scan-lite", {"code": data["code"]})

        return {
            "review": review.get("findings_json", "[]"),
            "vuln": vuln.get("findings_json", "[]"),
            "status": "ok",
        }

_skill = AuditSkill()

def set_node(node):
    _skill.set_node(node)

async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
```

## Healthcheck protocol

The `_healthcheck` convention is exactly that — a convention. No protocol changes, no new message types. Any skill receiving `{"_healthcheck": "true"}` in its input should respond with a lightweight probe instead of executing.

The base class handles this automatically:

```
Consumer sends: {"_healthcheck": "true", ...}

Leaf skill responds:
  {"status": "ok", "skill": "my-skill", "latency_ms": "12"}

Chain skill responds (pings all sub-skills first):
  {"status": "ok", "skill": "my-chain", "latency_ms": "421",
   "chain": "sub-a,sub-b", "chain_status": "warm"}

Unhealthy response:
  {"status": "unhealthy", "skill": "my-chain",
   "error": "sub-a: Connection refused", "latency_ms": "4100"}
```

**Side effect — model warm-up.** When a leaf skill's `healthcheck()` pings Ollama, that triggers model loading. By the time the real job arrives, the model is already in VRAM. The consumer perceives faster execution.

**Pre-exec vs periodic.** The chain skill's `check_chain()` is called inside `run()`, not automatically. This is intentional — the architect recommends `healthcheck_pre_exec = false` by default. The real value is in periodic monitoring (Phase 2: cron-driven health monitor + auto-delist). Pre-exec is belt-and-suspenders for when you want to be absolutely sure before committing expensive GPU time.

## Module export pattern

**Important:** The module-level `handle` and `set_node` must be local functions, not bound methods. This is because `serve_batch1.py` uses `inspect.getmodule(handler_fn)` to find `set_node` — if `handle` is a bound method, `getmodule` returns the base class module instead of your skill module, and `set_node` is never found.

```python
# CORRECT — local functions that delegate to the instance
_skill = MySkill()

def set_node(node):
    _skill.set_node(node)

async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
```

```python
# WRONG — bound methods, set_node will never be called
skill = MySkill()
handle = skill.handle      # inspect.getmodule() returns skill_base, not your module
set_node = skill.set_node  # never discovered
```

## Class reference

### `SkillBase`

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"unnamed-skill"` | Skill name (must match knarr.toml registration) |
| `chain` | `list[str]` | `[]` | Sub-skill dependencies (for chain healthcheck) |
| `required_fields` | `list[str]` | `[]` | Input fields validated before `run()` |
| `call_local_timeout` | `int` | `600000` | Default timeout (ms) for `self.call()` |

| Method | Override? | Description |
|--------|-----------|-------------|
| `run(data)` | **Yes** | Your skill logic. Return a dict. |
| `healthcheck()` | Optional | Probe your dependencies. Raise if unhealthy. |
| `handle(input_data)` | No | Entry point. Handles healthcheck, validation, timing, errors. |
| `set_node(node)` | No | Receives the DHTNode instance from the serve script. |
| `call(skill, input, timeout_ms)` | No | Convenience wrapper for `NODE.call_local()`. |
| `check_chain()` | No | Pings all `chain` skills. Returns `None` (healthy) or error string. |
| `node` | No | Property. Access the DHTNode. Raises if `set_node` wasn't called. |

## Quality Seal levels

The boilerplate maps directly to the Verein's Quality Seal:

| Level | What the boilerplate provides | What you add |
|-------|-------------------------------|--------------|
| **L0 (Bare)** | — | Raw `handle()` function, no base class |
| **L1 (Contained)** | Healthcheck, input validation, structured errors, timing | Docker isolation (`isolated = true` in knarr.toml) |
| **L2 (Hardened)** | Everything in L1 | Vault integration for secrets, periodic health monitor |
| **L3 (Auditable)** | Everything in L2 | Signed attestation, immutable audit log |

Using `SkillBase` gets you to L1 by default. The remaining levels require infrastructure configuration, not code changes.

## Files

```
sdk/
  README.md              # This file
  skill_base.py          # The base class
  examples/
    leaf_skill.py        # Minimal leaf skill example
    chain_skill.py       # Chain skill with healthcheck
    ollama_skill.py      # Leaf skill with Ollama dependency
```

## License

MIT
