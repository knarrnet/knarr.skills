# Cross-Model Code Review Prompt

Use this prompt with GPT-4 or Gemini. Paste all three source files after the prompt.

---

## Prompt

You are reviewing a Python plugin for a P2P agent network (knarr). The plugin is called "thrall" — an edge classifier that intercepts inbound messages and classifies them using a local small LLM (gemma3:1b, 778MB GGUF, CPU-only).

**Architecture context:**
- The plugin runs inside an asyncio event loop (single-threaded for async code)
- LLM inference runs in a ThreadPoolExecutor via `loop.run_in_executor()`
- The plugin has its own SQLite DB (`thrall.db`), separate from the node's DB
- The DB connection is synchronous sqlite3 (no async wrapper)
- The admin skill module shares the same DB connection object (passed by reference)
- Breaker files are JSON files on disk in a `breakers/` subdirectory
- The plugin does NOT send replies — it only classifies, records, and blocks

**Review focus areas:**

1. **Thread safety**: The LLM backend runs in a thread pool. The DB and in-memory dicts are accessed from the event loop. Is there a thread-safety gap where the executor thread could touch shared state?

2. **SQLite correctness**: Single connection, WAL mode, synchronous calls on the event loop. Any risk of blocking the event loop for too long? Any risk of corruption from concurrent access?

3. **Security**:
   - Can `from_node` (network-controlled input) be used for SQL injection, path traversal, or log injection?
   - Can a malicious node craft a message that causes the classifier to behave unexpectedly?
   - Can the prompt-load skill be abused if the whitelist is misconfigured?

4. **Memory**: OrderedDict with LRU eviction for reply_counter and solicited_sends. Plain dict for rate_limit. Any leak paths?

5. **Edge cases**:
   - What happens if thrall.db is deleted while the plugin is running?
   - What happens if a breaker file contains invalid JSON?
   - What happens if the LLM returns malformed JSON?
   - What happens if `on_shutdown` races with an in-flight classification?

6. **Correctness**:
   - Loop detection: Does the threshold logic work correctly? (threshold=2, fires on 3rd message)
   - Solicited detection: `record_send()` is a public method but must be called by the responder plugin. If it's never called, is the fallback behavior safe?
   - Ack detection in the prompt: Will gemma3:1b reliably classify "thanks for the update" as drop?

**Report format:**

```
## CRITICAL (must fix before deployment)
- [C-N] description, file:line

## WARNING (should fix, not blocking)
- [W-N] description, file:line

## GOOD (things done well)
- [G-N] description

## QUESTIONS (things to verify)
- [Q-N] question
```

---

## Files to review

Paste the contents of these three files below:
1. `thrall.py` (classification engine, ~275 lines)
2. `handler.py` (plugin hooks, ~623 lines)
3. `thrall_admin.py` (prompt management, ~121 lines)
