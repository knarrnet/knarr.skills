"""Event detection, filtering, and debounced queue for the agent."""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class AgentEvent:
    event_type: str          # "mail_received", "task_completed", "peer_change"
    event_key: str           # dedup key, e.g. "mail:{item_id}"
    data: Dict[str, Any]     # event payload
    timestamp: float = field(default_factory=time.time)


class EventFilter:
    """Applies configured filters to decide if an event should wake the agent."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config

    def should_accept_mail(self, from_node: str, msg_type: str) -> bool:
        mail_cfg = self._config.get("events", {}).get("mail_received", {})
        if not mail_cfg.get("enabled", False):
            return False

        ignore_types = mail_cfg.get("ignore_types", [])
        if msg_type in ignore_types:
            return False

        from_nodes = mail_cfg.get("from_nodes", [])
        if from_nodes and from_node not in from_nodes:
            return False

        msg_types = mail_cfg.get("msg_types", [])
        if msg_types and msg_type not in msg_types:
            return False

        return True

    def should_accept_task(self, skill_name: str, status: str) -> bool:
        task_cfg = self._config.get("events", {}).get("task_completed", {})
        if not task_cfg.get("enabled", False):
            return False

        skills = task_cfg.get("skills", [])
        if skills and skill_name not in skills:
            return False

        statuses = task_cfg.get("statuses", [])
        if statuses and status not in statuses:
            return False

        return True

    def should_accept_peer_change(self) -> bool:
        peer_cfg = self._config.get("events", {}).get("peer_change", {})
        return peer_cfg.get("enabled", False)


class EventQueue:
    """Debounced event queue. Collapses duplicate events within a time window."""

    def __init__(self, debounce_seconds: float = 30.0):
        self._debounce = debounce_seconds
        self._pending: Dict[str, AgentEvent] = {}  # key -> event
        self._seen_keys: Set[str] = set()

    def push(self, event: AgentEvent):
        self._pending[event.event_key] = event

    def drain(self) -> List[AgentEvent]:
        """Return events that have aged past the debounce window."""
        now = time.time()
        ready = []
        remaining = {}
        for key, event in self._pending.items():
            if (now - event.timestamp) >= self._debounce:
                ready.append(event)
                self._seen_keys.add(key)
            else:
                remaining[key] = event
        self._pending = remaining
        return ready

    def clear(self):
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


def extract_mail_items(msg_data: Dict[str, Any], our_node_id: str) -> List[Dict[str, Any]]:
    """Extract mail items addressed to us from a MailSync message."""
    items = msg_data.get("items", [])
    result = []
    for item in items:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(item, dict):
            continue
        to_node = item.get("to_node", "")
        if to_node == our_node_id:
            result.append(item)
    return result
