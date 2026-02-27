# knarr-thrall — Edge Classification Guard

## What it is

A plugin-level mail interceptor that classifies inbound messages using a local small model (gemma3:1b, 778MB GGUF, CPU-only). Same plugin pattern as the firewall — sits between the network and the node, classifies everything, records every decision.

## What it does

1. **Mail triage**: Classifies inbound mail as `wake` (forward to agent), `reply` (auto-handle), or `drop` (noise/spam). Trust tiers bypass the LLM for known nodes.

2. **Loop detection**: Tracks reply counts per session. Trips a granular circuit breaker when a bot-to-bot loop is detected. Wakes the agent.

3. **Transparent classification**: Every decision is recorded in `thrall_classifications` table with the original message, action, reasoning, and prompt hash. No silent drops. The agent can review and adjust.

4. **Granular breakers**: Circuit breakers scoped to node, skill, or node+skill combination. Auto-expire with TTL. Wake the agent on trip. Agent can remove breakers.

5. **Prompt security**: Classification prompts stored in local DB, pushed only via operator-whitelisted skill. No sidecar. No remote prompt injection.

## Performance (Phase 1.5 verified)

| Environment | Hot inference | Cold load | RAM |
|---|---|---|---|
| Docker (knarr-thrall:latest) | 600ms | 13.5s | 467MB |
| Hetzner CX23 (2 vCPU, 4GB) | 1.3-2.4s | 8.7s | 1.2GB |

CPU-only, zero GPU. Model loads once (singleton), stays resident.

## Files

| File | Purpose |
|---|---|
| `handler.py` | Plugin hooks (on_mail_received, on_tick). Breaker check, loop detection, classification recording. |
| `thrall.py` | Classification engine. EmbeddedBackend (llama-cpp-python) + OllamaBackend. Trust tier resolution. |
| `thrall_admin.py` | Prompt-load skill handler (operator-only). |
| `plugin.toml` | Configuration template with all knobs. |
| `Dockerfile.thrall` | Docker image extending knarr-node with llama-cpp-python + OpenBLAS. |

## Technical notes

- gemma3 requires multimodal content format: `[{"type": "text", "text": "..."}]` — plain strings fail
- `response_format={"type": "json_object"}` guarantees valid JSON, no fence stripping needed
- Dockerfile must keep `libgomp1` at runtime (OpenMP threading for llama.cpp)
- Build requires `pkg-config` for cmake BLAS detection
- v0.29.1 uses `mail_inbox` table, not `mail`

## Design decisions

- **Plugin, not core**: Thrall is optional. Remove it, node works normally. Same pattern as firewall.
- **In-process hooks**: Uses `on_mail_received`, not port redirect. If thrall crashes, mail flows through unclassified. No reachability dependency.
- **No stacking**: Plugin ordering achieves composability (02-thrall-triage, 03-thrall-skillgate).
- **Prompt is config**: Same 778MB model, different system prompt, different use case. Swap the prompt, change the behavior.

## Spec and review

- Implementation spec: `thing/decisions/IMPL-thrall-v2-classification-engine.md`
- Architectural review: `thing/decisions/REVIEW-thrall-release-spec.md`
- Phase 1.5 results: `docker/test-cluster/results/phase1.5-embedded-cpu.md`
