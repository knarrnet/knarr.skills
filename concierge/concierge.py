"""concierge — Protocol support through thrall concierge recipes.

Bridge skill for the concierge bundle. Routes to L0/L1/L2 recipes
based on the `tier` field (default: "faq").

Registered as three separate skills at three price points:
  concierge-faq-lite    (free)  → concierge-faq recipe
  concierge-expert-lite (3 cr)  → concierge-expert recipe
  concierge-intake-lite (8 cr)  → concierge-intake recipe

Input:
  - message: user's question (required)
  - tier: "faq", "expert", or "intake" (default: "faq", set via expose presets)

Output:
  - response: the concierge's answer
  - status: ok/error
  - tier: which tier handled the request
  - eval_type: llm/hotwire/cache/skip/bypass/error
  - wall_ms: pipeline time in milliseconds
"""

import json
import os
import sys
import time

_THRALL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "06-thrall",
)
if _THRALL_DIR not in sys.path:
    sys.path.insert(0, _THRALL_DIR)

NODE = None

def set_node(node):
    global NODE
    NODE = node

TIER_RECIPES = {
    "faq": "concierge-faq",
    "expert": "concierge-expert",
    "intake": "concierge-intake",
}

_engine = None
_evaluator = None
_db = None
_config = None


def _load_config():
    global _config
    if _config is not None:
        return _config
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open(os.path.join(_THRALL_DIR, "plugin.toml"), "rb") as f:
        _config = tomllib.load(f)
    return _config


def _get_engine():
    global _engine, _evaluator, _db

    if _engine is not None:
        return _engine

    from actions import ActionExecutor
    from backends import create_backend
    from db import ThrallDB
    from engine import PipelineEngine
    from evaluate import Evaluator
    from loader import load_all

    config = _load_config()
    thrall_cfg = config.get("config", {}).get("thrall", {})

    _db = ThrallDB(os.path.join(_THRALL_DIR, "thrall.db"))
    backend = create_backend(thrall_cfg)
    _evaluator = Evaluator(
        backend=backend,
        queue_timeout=thrall_cfg.get("queue_timeout", 5.0),
    )
    load_all(_THRALL_DIR, _db, _evaluator)
    actions = ActionExecutor(db=_db)

    _engine = PipelineEngine(
        db=_db,
        evaluator=_evaluator,
        action_executor=actions,
    )
    _engine.load_recipes()
    return _engine


async def handle(input_data: dict) -> dict:
    message = input_data.get("message", "").strip()
    if not message:
        return {"response": "Please enter a message.", "status": "error"}

    tier = input_data.get("tier", "faq").strip().lower()
    recipe_name = TIER_RECIPES.get(tier)
    if not recipe_name:
        return {"response": f"Unknown tier: {tier}", "status": "error"}

    try:
        engine = _get_engine()
    except Exception as e:
        return {"response": "Concierge unavailable.", "status": "error",
                "error": str(e)}

    from engine import Envelope

    envelope = Envelope(
        trigger_type="on_mail",
        timestamp=time.time(),
        fields={
            "from_node": "storefront",
            "to_node": "self",
            "msg_type": "text",
            "body_text": message,
            "body_json": json.dumps({"type": "text", "content": message}),
            "session_id": "",
        },
    )

    try:
        result = await engine.run(recipe_name, envelope)
    except Exception as e:
        return {"response": "Sorry, I couldn't process that right now.",
                "status": "error", "error": str(e)}

    if result.eval_result:
        response = result.eval_result.reason or "I'm not sure how to respond to that."
        eval_type = result.eval_result.eval_type
    else:
        response = "I couldn't process that right now."
        eval_type = "error"

    return {
        "response": response,
        "status": "ok",
        "tier": tier,
        "eval_type": eval_type,
        "wall_ms": str(result.wall_ms),
    }
