"""Prompt assembly for reactive events and scheduled jobs.

System prompt is assembled from markdown files in the prompts/ directory.
Files are loaded in sorted order and concatenated. Variables like {node_id},
{peer_count}, {skill_inventory} are substituted at runtime.

Operators customize behavior by editing these files. No code changes needed.
A scheduled job can regenerate skills.md when the inventory changes.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Load order for system prompt files
_PROMPT_FILES = [
    "identity.md",
    "router.md",
    "skills.md",
    "actions.md",
    "rules.md",
]


def _load_prompt_files(prompts_dir: Path) -> Optional[str]:
    """Load and concatenate prompt markdown files from directory.
    Returns None if directory doesn't exist or is empty."""
    if not prompts_dir.is_dir():
        return None

    parts = []
    # Load named files in defined order first
    for filename in _PROMPT_FILES:
        filepath = prompts_dir / filename
        if filepath.exists():
            parts.append(filepath.read_text(encoding="utf-8").strip())

    # Then load any extra *.md files not in the standard list (operator additions)
    standard = set(_PROMPT_FILES)
    extras = sorted(f for f in prompts_dir.glob("*.md") if f.name not in standard)
    for filepath in extras:
        parts.append(filepath.read_text(encoding="utf-8").strip())

    if not parts:
        return None

    return "\n\n".join(parts)


def format_skill_inventory(skills: List[Dict[str, Any]]) -> str:
    """Format skill inventory for injection into system prompt."""
    if not skills:
        return "(no skills loaded)"
    lines = []
    for s in skills:
        name = s.get("name", "unknown")
        price = s.get("price", 0)
        desc = s.get("description", "")[:80]
        lines.append(f"- **{name}** ({price} credits): {desc}")
    return "\n".join(lines)


def assemble_system_prompt(template: str, node_id: str, peer_count: int,
                           skill_inventory: Optional[List[Dict[str, Any]]] = None,
                           prompts_dir: Optional[Path] = None) -> str:
    """Assemble system prompt from files (preferred) or template fallback."""
    inventory_str = format_skill_inventory(skill_inventory or [])

    # Try loading from prompt files first
    if prompts_dir:
        file_content = _load_prompt_files(prompts_dir)
        if file_content:
            try:
                return file_content.format(
                    node_id=node_id[:16],
                    peer_count=peer_count,
                    skill_inventory=inventory_str,
                )
            except KeyError as e:
                log.warning(f"Prompt file variable error: {e}, falling back to template")

    # Fallback to plugin.toml template
    return template.format(
        node_id=node_id[:16],
        peer_count=peer_count,
        skill_inventory=inventory_str,
    )


def assemble_mail_prompt(template: str, event_data: Dict[str, Any],
                         conversation_history: List[Dict]) -> str:
    body = event_data.get("body", {})
    if isinstance(body, dict):
        body_str = json.dumps(body, ensure_ascii=False)[:1000]
    else:
        body_str = str(body)[:1000]

    history_lines = []
    for msg in conversation_history[-5:]:
        direction = msg.get("direction", "?")
        node = msg.get("from_node", "?")[:16]
        text = msg.get("body", "")[:200]
        history_lines.append(f"  [{direction}] {node}: {text}")
    history_str = "\n".join(history_lines) if history_lines else "(none)"

    return template.format(
        from_node=event_data.get("from_node", "unknown")[:16],
        msg_type=event_data.get("msg_type", "unknown"),
        session_id=event_data.get("session_id", ""),
        body=body_str,
        conversation_history=history_str,
    )


def assemble_task_prompt(template: str, event_data: Dict[str, Any],
                         recent_stats: str) -> str:
    return template.format(
        skill_name=event_data.get("skill_name", "unknown"),
        status=event_data.get("status", "unknown"),
        wall_time_ms=event_data.get("wall_time_ms", 0),
        error=event_data.get("error", "none")[:500],
        recent_stats=recent_stats,
    )


def assemble_job_prompt(template: str, context: Dict[str, Any]) -> str:
    """Fill a scheduled job prompt template with computed context."""
    try:
        return template.format(**context)
    except KeyError:
        return template.format_map(_SafeDict(context))


class _SafeDict(dict):
    def __missing__(self, key):
        return f"{{{key}}}"
