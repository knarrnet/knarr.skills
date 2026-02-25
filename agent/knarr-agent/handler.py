"""knarr-agent: Node-resident autonomous agent plugin.

Two modes:
  1. Reactive — individual events (mail, task completion) wake the agent
  2. Scheduled — periodic jobs pull bulk data and analyze trends
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from knarr.core.messages import Message, MailSync
from knarr.core.models import NodeInfo
from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth

from events import AgentEvent, EventFilter, EventQueue, extract_mail_items
from llm import LLMBackend, create_backend
from memory import AgentMemory
from actions import ActionExecutor
from prompts import (
    assemble_system_prompt, assemble_mail_prompt,
    assemble_task_prompt, assemble_job_prompt,
)
from scheduler import Scheduler


def _load_skill_inventory(plugin_dir: Path) -> List[Dict[str, Any]]:
    """Load announced/public skills from knarr.toml for prompt injection."""
    # Walk up from plugin dir to find knarr.toml
    toml_path = None
    candidate = plugin_dir.parent
    for _ in range(5):
        p = candidate / "knarr.toml"
        if p.exists():
            toml_path = p
            break
        candidate = candidate.parent

    if not toml_path:
        return []

    try:
        import tomllib
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return []

    skills = cfg.get("skills", {})
    inventory = []
    for name, skill_cfg in skills.items():
        if not isinstance(skill_cfg, dict):
            continue
        vis = skill_cfg.get("visibility", "announced")
        if vis in ("public", "announced"):
            inventory.append({
                "name": name,
                "price": skill_cfg.get("price", 0),
                "description": skill_cfg.get("description", ""),
            })
    return inventory


class AgentPlugin(PluginHooks):
    def __init__(self, ctx: PluginContext, config: Dict[str, Any]):
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._enabled = config.get("enabled", True)
        self._debug = config.get("debug", False)

        if not self._enabled:
            self._log.info("Agent plugin disabled by config")
            return

        # Memory
        db_path = ctx.plugin_dir / "agent.db"
        self._memory = AgentMemory(db_path)

        # Event handling
        self._filter = EventFilter(config)
        debounce = float(config.get("event_debounce_seconds", 30))
        self._queue = EventQueue(debounce)

        # LLM
        self._llm: LLMBackend = create_backend(config, vault_get=ctx.vault_get)

        # Inject cockpit token for skill calls
        cockpit_token = ""
        if ctx.vault_get:
            try:
                cockpit_token = ctx.vault_get("cockpit_token") or ""
            except Exception:
                pass
        config_with_token = dict(config)
        config_with_token["cockpit_token"] = cockpit_token

        # Actions
        self._actions = ActionExecutor(
            config=config_with_token,
            memory=self._memory,
            send_mail=ctx.send_mail,
            group_engine=ctx.group_engine,
            node_id=ctx.node_id,
            plugin_log=ctx.log,
        )

        # Scheduler
        self._scheduler = Scheduler(config, self._memory)

        # Rate limits
        self._max_llm_per_hour = int(config.get("max_llm_calls_per_hour", 30))

        # Tick control
        self._tick_multiplier = int(config.get("tick_interval_multiplier", 6))
        self._tick_count = 0

        # Singleflight
        self._processing = False

        # State for event detection
        self._last_exec_log_id = int(self._memory.get_state("exec_log_cursor", "0"))
        self._last_mail_rowid = int(self._memory.get_state("mail_cursor", "0"))
        self._known_peers: Set[str] = set()

        # Skill inventory (loaded once at startup)
        self._skill_inventory = _load_skill_inventory(ctx.plugin_dir)

        # Prompt files directory
        self._prompts_dir = ctx.plugin_dir / "prompts"

        self._log.info(
            f"Agent plugin initialized: backend={config.get('llm_backend', 'static')}, "
            f"debounce={debounce}s, tick_mult={self._tick_multiplier}, "
            f"skills_loaded={len(self._skill_inventory)}"
        )

    # ── on_inbound: intercept MailSync for reactive mail events ──

    async def on_inbound(self, msg: Message, peer_ip: str) -> bool:
        """Always returns True — pure observer, never blocks messages."""
        if not self._enabled:
            return True

        try:
            if isinstance(msg, MailSync):
                self._handle_mail_sync(msg)
        except Exception as e:
            self._log.error(f"Agent on_inbound error: {e}")

        return True

    def _handle_mail_sync(self, msg: MailSync):
        """Extract mail items addressed to us, filter, and push to event queue."""
        items = extract_mail_items(msg.to_dict(), self._ctx.node_id)
        for item in items:
            from_node = item.get("from_node", "")
            body = item.get("body", {})
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    body = {"content": body}

            msg_type = item.get("msg_type", "text")
            if not self._filter.should_accept_mail(from_node, msg_type):
                if self._debug:
                    self._log.debug(f"Agent: filtered mail from={from_node[:16]} type={msg_type}")
                continue

            item_id = item.get("item_id", str(time.time()))
            event = AgentEvent(
                event_type="mail_received",
                event_key=f"mail:{item_id}",
                data={
                    "from_node": from_node,
                    "msg_type": msg_type,
                    "body": body,
                    "session_id": item.get("session_id", ""),
                    "item_id": item_id,
                },
            )
            self._queue.push(event)

            # Record inbound conversation
            session_id = item.get("session_id", "")
            if session_id:
                self._memory.add_conversation(
                    session_id=session_id,
                    from_node=from_node,
                    direction="inbound",
                    body=json.dumps(body)[:500],
                )

            if self._debug:
                self._log.debug(f"Agent: queued mail event from={from_node[:16]} type={msg_type}")

    # ── on_tick: run agent loop on configured interval ──

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        if not self._enabled:
            return

        self._tick_count += 1
        if self._tick_count % self._tick_multiplier != 0:
            return

        if self._processing:
            if self._debug:
                self._log.debug("Agent: skipping tick, still processing")
            return

        self._processing = True
        try:
            await self._agent_tick(peers)
        except Exception as e:
            self._log.error(f"Agent tick error: {e}", exc_info=True)
        finally:
            self._processing = False

    async def _agent_tick(self, peers: List[NodeInfo]):
        """Main agent loop: detect events, drain queue, run scheduled jobs."""
        self._log.info(f"Agent tick #{self._tick_count}: queue={self._queue.pending_count} peers={len(peers)}")

        # ── Reactive: poll mail table in node.db (catches self-delivery + MailSync) ──
        if self._config.get("events", {}).get("mail_received", {}).get("enabled", False):
            self._poll_mail_table()

        # ── Reactive: detect task completions from execution_log ──
        if self._config.get("events", {}).get("task_completed", {}).get("enabled", False):
            self._poll_execution_log()

        # ── Reactive: detect peer changes ──
        if self._filter.should_accept_peer_change():
            self._detect_peer_changes(peers)

        # ── Reactive: drain event queue ──
        events = self._queue.drain()
        for event in events:
            await self._process_event(event)

        # ── Scheduled: check for due jobs ──
        due_jobs = self._scheduler.get_due_jobs()
        for job in due_jobs:
            await self._run_scheduled_job(job)

    def _poll_execution_log(self):
        """Check execution_log in node.db for new completions since our cursor."""
        storage_path = self._ctx.storage_path
        if not storage_path:
            return

        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{storage_path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT id, skill_name, status, wall_time_ms, error, created_at "
                "FROM execution_log WHERE id > ? ORDER BY id ASC LIMIT 50",
                (self._last_exec_log_id,)
            ).fetchall()
            conn.close()
        except Exception as e:
            self._log.error(f"Agent: failed to poll execution_log: {e}")
            return

        for row in rows:
            row_id, skill_name, status, wall_time_ms, error, created_at = row
            self._last_exec_log_id = row_id

            if not self._filter.should_accept_task(skill_name, status or ""):
                continue

            event = AgentEvent(
                event_type="task_completed",
                event_key=f"task:{row_id}",
                data={
                    "skill_name": skill_name,
                    "status": status,
                    "wall_time_ms": wall_time_ms or 0,
                    "error": error or "",
                    "created_at": created_at,
                },
            )
            self._queue.push(event)

        # Persist cursor
        self._memory.set_state("exec_log_cursor", str(self._last_exec_log_id))

    def _poll_mail_table(self):
        """Poll mail inbox in node.db for new messages since our cursor.
        Supports both v0.29.1+ (mail_inbox) and legacy (mail) table layouts.
        This catches ALL delivery paths including self-delivery."""
        storage_path = self._ctx.storage_path
        if not storage_path:
            return

        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{storage_path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")

            # Detect table: prefer mail_inbox (v0.29.1+), fall back to mail
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('mail_inbox','mail')"
            ).fetchall()}
            table = "mail_inbox" if "mail_inbox" in tables else "mail"

            rows = conn.execute(
                f"SELECT rowid, message_id, from_node, body, session_id, msg_type "
                f"FROM {table} WHERE rowid > ? AND to_node = ? AND system = 0 "
                f"ORDER BY rowid ASC LIMIT 50",
                (self._last_mail_rowid, self._ctx.node_id)
            ).fetchall()
            conn.close()
        except Exception as e:
            self._log.error(f"Agent: failed to poll mail table: {e}")
            return

        for row in rows:
            rowid, message_id, from_node, body_str, session_id, msg_type = row
            self._last_mail_rowid = rowid

            if not self._filter.should_accept_mail(from_node, msg_type or "text"):
                continue

            # EventQueue deduplicates by event_key — if on_inbound already
            # pushed this mail via MailSync, the queue replaces it (same key).
            event_key = f"mail:{message_id}"

            body = {}
            try:
                body = json.loads(body_str) if body_str else {}
            except (json.JSONDecodeError, TypeError):
                body = {"content": str(body_str)[:500]}

            event = AgentEvent(
                event_type="mail_received",
                event_key=event_key,
                data={
                    "from_node": from_node,
                    "msg_type": msg_type or "text",
                    "body": body,
                    "session_id": session_id or "",
                    "item_id": message_id,
                },
            )
            self._queue.push(event)

            # Record inbound conversation
            if session_id:
                self._memory.add_conversation(
                    session_id=session_id,
                    from_node=from_node,
                    direction="inbound",
                    body=json.dumps(body)[:500],
                )

        # Persist cursor
        self._memory.set_state("mail_cursor", str(self._last_mail_rowid))

    def _detect_peer_changes(self, peers: List[NodeInfo]):
        """Diff current peer set against known peers."""
        current = {p.node_id for p in peers}
        if not self._known_peers:
            self._known_peers = current
            return

        joined = current - self._known_peers
        left = self._known_peers - current

        for node_id in joined:
            event = AgentEvent(
                event_type="peer_change",
                event_key=f"peer_join:{node_id[:16]}",
                data={"change": "joined", "node_id": node_id},
            )
            self._queue.push(event)

        for node_id in left:
            event = AgentEvent(
                event_type="peer_change",
                event_key=f"peer_left:{node_id[:16]}",
                data={"change": "left", "node_id": node_id},
            )
            self._queue.push(event)

        self._known_peers = current

    # ── Process a single reactive event ──

    async def _process_event(self, event: AgentEvent):
        """Rate-check, assemble prompt, call LLM, execute action, log."""
        if not self._memory.check_rate_limit("llm_calls", self._max_llm_per_hour):
            self._log.warning("Agent: LLM rate limit hit, logging event without LLM")
            self._memory.log_event(event.event_type, event.event_key, event.data,
                                   {"action": "log", "reason": "rate_limited"})
            return

        # Assemble prompts
        peer_count = len(self._ctx.get_peers())
        system_template = self._config.get("prompts", {}).get("system", "Respond with JSON.")
        system_prompt = assemble_system_prompt(
            system_template, self._ctx.node_id, peer_count,
            skill_inventory=self._skill_inventory,
            prompts_dir=self._prompts_dir,
        )

        if event.event_type == "mail_received":
            template = self._config.get("prompts", {}).get("mail_received", "{body}")
            history = self._memory.get_conversation(event.data.get("session_id", ""), limit=5)
            user_prompt = assemble_mail_prompt(template, event.data, history)
        elif event.event_type == "task_completed":
            template = self._config.get("prompts", {}).get("task_completed", "{skill_name}: {status}")
            user_prompt = assemble_task_prompt(template, event.data, "N/A")
        else:
            user_prompt = json.dumps(event.data)

        # Call LLM
        try:
            decision = await self._llm.generate(system_prompt, user_prompt)
        except Exception as e:
            self._log.error(f"Agent LLM call failed: {e}")
            decision = {"action": "log", "summary": f"LLM error: {e}"}

        if self._debug:
            self._log.debug(f"Agent decision for {event.event_type}: {json.dumps(decision)[:200]}")

        # Execute
        result = await self._actions.execute(decision, event.data)

        # Log
        self._memory.log_event(
            event.event_type, event.event_key, event.data,
            {"decision": decision, "result": result},
        )

    # ── Run a scheduled job ──

    async def _run_scheduled_job(self, job):
        """Pull bulk data, assemble context, call LLM, execute, mark ran."""
        if not self._memory.check_rate_limit("llm_calls", self._max_llm_per_hour):
            self._log.warning(f"Agent: LLM rate limit hit, skipping job {job.name}")
            return

        self._log.info(f"Agent: running scheduled job '{job.name}'")

        # Pull context based on job name
        storage_path = self._ctx.storage_path or ""
        if job.name == "task_stats":
            context = self._scheduler.pull_task_stats(storage_path, job.window_hours)
        elif job.name == "daily_digest":
            context = self._scheduler.pull_daily_digest(storage_path, self._memory)
        else:
            context = {"info": f"Unknown job: {job.name}"}

        # Assemble prompts
        peer_count = len(self._ctx.get_peers())
        system_template = self._config.get("prompts", {}).get("system", "Respond with JSON.")
        system_prompt = assemble_system_prompt(
            system_template, self._ctx.node_id, peer_count,
            skill_inventory=self._skill_inventory,
            prompts_dir=self._prompts_dir,
        )
        user_prompt = assemble_job_prompt(job.prompt_template, context)

        # Call LLM
        try:
            decision = await self._llm.generate(system_prompt, user_prompt)
        except Exception as e:
            self._log.error(f"Agent job '{job.name}' LLM error: {e}")
            decision = {"action": "log", "summary": f"Job {job.name} LLM error: {e}"}

        # Execute
        result = await self._actions.execute(decision, {"job": job.name})

        # Log
        self._memory.log_event(
            f"job:{job.name}", None, context,
            {"decision": decision, "result": result},
        )

        # Mark ran
        self._scheduler.mark_ran(job)
        self._log.info(f"Agent: job '{job.name}' completed: {result.get('status', 'unknown')}")

    # ── Shutdown ──

    async def on_shutdown(self) -> None:
        if self._enabled:
            self._log.info("Agent plugin shutting down")
            # Persist final cursors
            self._memory.set_state("exec_log_cursor", str(self._last_exec_log_id))
            self._memory.set_state("mail_cursor", str(self._last_mail_rowid))
