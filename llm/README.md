# LLM Skills

GPU-powered inference skills for the Knarr network.

## Skills

| Skill | Price | Description |
|-------|-------|-------------|
| `llm-toolcall-lite` | 2.0 | Serverless LLM inference with tool calling. Send a package (prompt, tools, data), get reasoned output. |

## llm-toolcall-lite

The caller sends a complete execution package — the skill runs the Ollama tool-call loop internally. One API call from the caller, multi-turn reasoning internally.

**Input**:
- `system_prompt` — Agent personality, instructions, world context
- `user_input` — The task or question
- `tools_json` — Tool definitions in OpenAI format (JSON array)
- `tool_data_json` — Pre-loaded data for each tool, keyed by tool name
- `world` — Optional persistent context (merged into system prompt)
- `model` — Model preference: `qwen3:14b` (default), `gemma3:12b`, `llama3.2:3b`, `deepseek-r1:14b`
- `temperature`, `max_tokens`, `max_rounds` — Optional tuning

**Output**:
- `result` — Final LLM response
- `rounds` — Number of reasoning rounds
- `tool_calls_made` — JSON log of all tool calls and results
- `model`, `wall_time_ms` — Execution metadata

### Requirements

- Ollama running locally with at least one supported model
- No external API keys needed — all inference is local GPU
