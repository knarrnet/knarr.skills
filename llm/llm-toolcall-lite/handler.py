"""llm-toolcall-lite — Serverless LLM inference with tool calling.

Caller sends a complete execution package:
  - system_prompt: agent personality, world context, instructions
  - user_input: the task/question
  - tools_json: tool definitions (OpenAI-compatible format)
  - tool_data_json: pre-loaded data for each tool (the "food")
  - model: optional model preference

The skill runs the Ollama tool-call loop internally:
  LLM reasons → calls tool → we look up data in food → feed back → repeat.

One call from the caller's perspective, multi-turn internally.
"""

import json
import os
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

NODE = None
OLLAMA_URL = "http://localhost:11434/api/chat"

# Models available for callers to request
ALLOWED_MODELS = {
    "qwen3:14b", "qwen3:32b-q8_0", "gemma3:12b", "llama3.2:3b",
    "deepseek-r1:14b", "qwen3-embedding:8b",
}
DEFAULT_MODEL = "qwen3:14b"

# Safety limits
MAX_ROUNDS = 10
MAX_INPUT_CHARS = 50000
MAX_TOOL_DATA_CHARS = 100000


def set_node(node):
    global NODE
    NODE = node


def _call_ollama_chat(model, messages, tools=None, temperature=0.3, max_tokens=2048):
    """Call Ollama chat API with optional tool definitions."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode()
    req = Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=120)
    return json.loads(resp.read())


def _resolve_tool_call(tool_name, arguments, tool_data):
    """Look up tool result from pre-loaded data.

    tool_data is keyed by tool name. The value is the "food" — a string blob
    of data that the tool has access to. We return it wholesale and let the
    LLM extract what it needs based on the arguments.
    """
    if tool_name not in tool_data:
        return json.dumps({"error": f"Tool '{tool_name}' not found in provided data"})

    food = tool_data[tool_name]

    # If food is a dict, try argument-based lookup first
    if isinstance(food, dict):
        # Try matching first argument value
        for arg_val in arguments.values():
            if str(arg_val) in food:
                return str(food[str(arg_val)])
        # Fall back to returning the whole dict as JSON
        return json.dumps(food, ensure_ascii=False)

    # String food — return as-is
    return str(food)


async def handle(input_data: dict) -> dict:
    t0 = time.time()

    # --- Parse inputs ---
    system_prompt = input_data.get("system_prompt", "").strip()
    user_input = input_data.get("user_input", "").strip()
    if not user_input:
        return {"status": "error", "error": "user_input is required"}

    model = input_data.get("model", DEFAULT_MODEL).strip()
    if model not in ALLOWED_MODELS:
        model = DEFAULT_MODEL

    temperature = float(input_data.get("temperature", "0.3"))
    max_tokens = int(input_data.get("max_tokens", "2048"))
    max_rounds = min(int(input_data.get("max_rounds", "5")), MAX_ROUNDS)

    # Parse tools (OpenAI-compatible format)
    tools = []
    tools_raw = input_data.get("tools_json", "").strip()
    if tools_raw:
        try:
            tools = json.loads(tools_raw)
            if not isinstance(tools, list):
                tools = [tools]
        except json.JSONDecodeError:
            return {"status": "error", "error": "tools_json must be valid JSON array"}

    # Parse tool data (the "food")
    tool_data = {}
    data_raw = input_data.get("tool_data_json", "").strip()
    if data_raw:
        if len(data_raw) > MAX_TOOL_DATA_CHARS:
            return {"status": "error", "error": f"tool_data_json exceeds {MAX_TOOL_DATA_CHARS} chars"}
        try:
            tool_data = json.loads(data_raw)
            if not isinstance(tool_data, dict):
                return {"status": "error", "error": "tool_data_json must be a JSON object keyed by tool name"}
        except json.JSONDecodeError:
            return {"status": "error", "error": "tool_data_json must be valid JSON"}

    # World context (merged into system prompt)
    world = input_data.get("world", "").strip()
    if world:
        system_prompt = f"{system_prompt}\n\n## World Context\n{world}" if system_prompt else f"## World Context\n{world}"

    # Input size guard
    total_input = len(system_prompt) + len(user_input) + len(data_raw)
    if total_input > MAX_INPUT_CHARS + MAX_TOOL_DATA_CHARS:
        return {"status": "error", "error": "Total input size too large"}

    # --- Build initial messages ---
    messages = []
    if system_prompt:
        # Prepend /no_think for qwen models to suppress thinking tags
        sp = f"/no_think {system_prompt}" if "qwen" in model else system_prompt
        messages.append({"role": "system", "content": sp})
    messages.append({"role": "user", "content": user_input})

    # --- Tool-call loop ---
    tool_calls_log = []
    rounds = 0

    for rounds in range(1, max_rounds + 1):
        try:
            response = _call_ollama_chat(
                model, messages,
                tools=tools if tools else None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            return {"status": "error", "error": f"Ollama call failed: {e}", "rounds": str(rounds)}

        msg = response.get("message", {})

        # Check if model wants to call tools
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            # No tool calls — this is the final response
            final_text = msg.get("content", "")
            break
        else:
            # Model wants to use tools — resolve each one
            messages.append(msg)  # Add assistant's tool call message

            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                arguments = func.get("arguments", {})

                # Look up result in provided food
                result = _resolve_tool_call(tool_name, arguments, tool_data)

                tool_calls_log.append({
                    "round": rounds,
                    "tool": tool_name,
                    "args": arguments,
                    "result_preview": result[:200],
                })

                # Feed result back to LLM
                messages.append({
                    "role": "tool",
                    "content": result,
                })
    else:
        # Hit max rounds without final response
        final_text = msg.get("content", "(max tool call rounds reached)")

    t_total = time.time() - t0

    return {
        "status": "ok",
        "result": final_text,
        "model": model,
        "rounds": str(rounds),
        "tool_calls_made": json.dumps(tool_calls_log),
        "wall_time_ms": str(int(t_total * 1000)),
    }
