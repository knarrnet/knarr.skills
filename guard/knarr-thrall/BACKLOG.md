# knarr-thrall Backlog (next sprint)

## Current scope (v2)
Mail guard only: classifies inbound messages, records decisions, detects loops, trips breakers.
No skill interception, no cockpit API access, no outbound filtering, single model.

**Shipped in v2:** transparent classification, granular breakers, loop detection (session + sessionless),
knock-pattern detection, prompt security (admin skill + whitelist), batched DB commits,
classification TTL (30-day prune), thread-safe inference lock.

---

## Tier 1 — Quick wins (improve what exists)

### 1. Response Cache
Simple in-memory cache: `(prompt_hash, tier, body_hash) -> classification` with TTL.
Catches "agreed", "thanks", repeated standardized messages. Critical when stacking thralls.
Key: tier must be in the cache key (prompt includes {tier} placeholder).

### 2. Hotwire Rules (Priority-based bypass)
Agent can set priority-based rules alongside the LLM:
- Priority 100: Agent override hotwires (skip LLM — "always wake this node")
- Priority 50: LLM triage (current behavior)
- Priority 10: Default fallback hotwires (catch when LLM is slow/down)
Agent adds/removes dynamically. Higher priority wins.

### 3. FAQ / Quickstart Responder (reply action)
Thrall answers `reply`-classified messages himself using gemma3:1b + a loaded FAQ prompt.
No Claude needed for "/help", config questions, getting-started queries.
Three tiers of intelligence: trust bypass (0ms) -> thrall FAQ (1s, free) -> Claude (expensive).
FAQ content loaded via prompt DB (same admin skill, hot-swappable).
Basically the README + quickstart guides stuffed into a reply_prompt.

### 4. Triage Queue
Bounded in-memory queue decouples message arrival from LLM classification.
Messages land in queue instantly (microseconds), worker drains sequentially
(model isn't thread-safe anyway — one call at a time).
- If queue full: fall back to tier-based default (same as LLM timeout fallback)
- Backpressure metric: queue depth tells you if the model can't keep up
- Cache hits (#1) and hotwire matches (#2) bypass the queue entirely
- Only messages that genuinely need LLM classification enter the queue
Without this, second message blocks on inference lock until first completes.

### 5. Rate Limiter Activation
`_record_rate()` exists but is never called, so `_check_rate()` always sees empty windows.
Also: rate_limit dict leaks entries for inactive nodes — `on_tick` only removes empty lists,
never ages timestamps for keys that stop sending.
Fix: wire up `_record_rate()` in the classification path, add timestamp aging.

### 6. Log Watcher
Thrall monitors node logs (serve log, skill logs, error output) in real-time.
Detects: crash patterns, repeated errors, stuck loops, resource exhaustion.
Alerts agent via injected mail on anomalies. Even just a console-debug-log watcher adds value.
Use case: overnight autonomous operation — thrall catches errors the agent would miss.

### 7. Shutdown Race Fix
`on_shutdown` closes DB while in-flight classification in thread pool may still complete
and try to write. Fix: wait for in-flight work to drain before closing DB.
Also: breaker file I/O is synchronous on every message — move to async or cache in memory.

---

## Tier 2 — Expand thrall's reach

### 8. Skill Interception
Thrall hooks into skill execution path — not just mail. Inspect inbound skill calls
before they execute. Gate by sender tier, input content, skill name.
Use case: block suspicious skill calls from unknown nodes without waking the agent.
Requires: `on_task_execute` hook in PluginHooks (Forseti will design after Tier 1 proven).
Forseti's review: DEFERRED — needs core hook. Build after Tier 1 shipped.

### 9. Cockpit API Access
Thrall can query the cockpit — read economy state, peer info, job history.
Use case: "is this sender in debt?" -> auto-drop. "Has this peer been sanctioned?" -> block.
Context-aware triage: decisions based on ledger state, not just message content.
Requires: cockpit client in PluginContext or direct HTTP to localhost.

### 10. Multi-Model Support
Load multiple models for different tasks. Examples:
- gemma3:1b for fast triage (current)
- A code-aware model for skill input validation
- A larger model for complex FAQ responses
Model selection by task type. Lazy-loaded, shared singleton per model.

### 11. Targeted Prompts (per-tier / per-sender)
Different classification prompts for different tiers or specific senders.
"Known" nodes get a prompt tuned for collaboration context.
"Unknown" nodes get a stricter spam-filtering prompt.
Specific nodes can have custom prompts (e.g., knarrbot gets a "filter ack noise" prompt).
Builds on existing prompt DB — add tier/node_prefix column to thrall_prompts table.

### 12. NL Command Extraction
Natural language commands to the thrall: "Add member NODEID to Felag ABC",
"add NODEID to Blacklist", "Change Tier of XXX".
Thrall extracts values (entity, action, target) via LLM, calls predefined APIs.
Any agent can call — data is extracted and a pre-defined tool is invoked.
On failure: thrall sends an injected mail to the agent with the error.
Pattern: NL in -> structured extraction -> API call -> success/fail response.

### 13. Mail Endpoint Concurrency Guard
Two agents hitting the mail endpoint simultaneously (Samim's scenario).
Thrall serializes or arbitrates concurrent access to prevent race conditions,
duplicate processing, or message loss when multiple agents poll/send at once.
Use case: shared node with multiple agent plugins, or external MCP clients.

### 14. MCP Call Guardrails
External agents route MCP calls through the thrall before they reach the node.
Same classification as mail but for tool/skill invocations via MCP protocol.
Catches: unauthorized MCP access, malformed calls, rate abuse from external clients.
Patrick: "having MCP-calls from external agents routed through the Thrall"

---

## Tier 3 — Thrall as a full guardian

### 15. Skill Chain Health Check
Thrall pre-validates skill chains before execution. Checks:
- Are all chained skills available? (skill discovery check)
- Are dependent services up? (ollama, GPU, external APIs)
- Is the expected execution time within timeout?
Returns early failure instead of burning credits on a chain that will fail at step 3.
Requires: skill registry access + health probe per skill.

### 16. Job Report Interception
Thrall reads inbound skill job reports before they reach the node/agent.
Filters noise: "task completed successfully" acks don't need to wake anyone.
Flags anomalies: failed jobs, unexpected outputs, cost overruns.
Use case: 100 job reports/hour from a busy skill chain — only escalate failures.
Requires: `on_jobreport_received` hook or intercept in mail handler.

### 17. Outbound Guardrail
Thrall inspects OUTGOING messages/responses before they leave the node.
Catches: secrets leaking in responses, accidentally verbose error messages,
PII in skill outputs, malformed protocol messages.
The agent composes a reply -> thrall reviews it -> pass/redact/block.
Requires: `on_mail_send` or `on_skill_response` hook (core change).
Critical for autonomous agents — the guardrail that prevents the agent from saying something stupid.

### 18. Smart Router / Load Balancer
Thrall acts as a smart router, distributing work across multiple operator nodes
by skill calls or mail. Could shape HA-load balancers across the network.
Patrick: "This little thing could act as a smart router dealing out work to thousands
of operators by skill calls or mail. If it is compromised or does not work — no harm done."

### 19. Own 1-Hop Skills
Thrall exposes its own skills that terminate on itself: `triage-lite`, `health-check-lite`,
`stats-lite`. Whitelisted to specific node IDs. No chaining through the network,
no credit leakage. The worker node stays shielded.
Forseti: Already possible — no core changes needed.

### 20. Prompt Repository
Model and prompt repository: thrall can chain self-calls with the model and the prompt
and the context in an artifact. Prompts stored as sidecar artifacts, hot-swappable.
Enables prompt versioning, A/B testing, rollback.
Patrick: "A thrall could chain self-calls with the model and the prompt and the context in an artifact."

---

## Deployment & Packaging

### 21. Pip-installable Package
`pip install knarr-thrall` — registers itself as a plugin. Clean deployment story.
Currently deployed by file copy. Proper packaging enables: version pinning,
dependency management, automated upgrades, VPS provisioning via cloud-init.

---

## Design Decisions (agreed)

| ID | Decision | Source |
|----|----------|--------|
| D-001 | Thrall is a PLUGIN, not core. Same pattern as firewall. | Forseti review |
| D-002 | No port redirect. If thrall crashes, node becomes unreachable. Use `on_mail_received` hooks instead. | Forseti review (rejected) |
| D-003 | No explicit stacking infra. Plugin ordering (`02-thrall-triage`, `03-thrall-skillgate`) achieves composability. YAGNI. | Forseti review (rejected) |
| D-004 | Skill gate deferred until `on_task_execute` hook is designed. | Forseti review |
| D-005 | Pass-through queue already possible, no core changes needed. | Forseti review |
| D-006 | Three tiers of intelligence: trust bypass (0ms) -> thrall FAQ (1s, free) -> Claude (expensive). | Architecture discussion |
| D-007 | Prompt stored as config artifact (sidecar, hot-swappable via admin skill). | Architecture discussion |
| D-008 | Single model per thrall instance (v2). Multi-model is Tier 2. | Sprint scoping |
| D-009 | Classification records have TTL (30-day) + pruning. Shipped in v2. | Forseti review |
