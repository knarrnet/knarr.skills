# knarr-agent — DEPRECATED

**Do not use.** This plugin has been removed.

knarr-agent was an early prototype for node-resident intelligence. It has been
fully superseded by [knarr-thrall](../guard/knarr-thrall/), which provides:

- Two-stage LLM cascade (L1 fast filter + L2 full classification)
- TOML-based recipe engine (no code changes needed for new behaviors)
- Multiple backends (local CPU / ollama / OpenAI-compatible)
- Trust tiers with per-sender classification
- Hotwire evaluation (zero LLM cost for operational recipes)
- Settlement identity with delegated signing
- Bus event integration for real-time monitoring

**Why it was removed:**

- No loop protection — an unguarded LLM call chain burned 20M tokens in 2 minutes
- No cascade — every message hit the full LLM, no fast pre-filter
- Namespace collision — `actions.py` conflicts with any other plugin using the same module name
- No trust tiers — no way to bypass or block senders before LLM evaluation
- Hardcoded Python behaviors instead of hot-reloadable TOML recipes

**If you have knarr-agent installed:** Remove the plugin directory and switch to
knarr-thrall. Do not run both simultaneously.
