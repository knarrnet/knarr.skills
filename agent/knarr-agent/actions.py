"""Action executor: validates LLM decisions and dispatches them."""

import json
import logging
import time
from typing import Any, Callable, Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from memory import AgentMemory

log = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(self, config: Dict[str, Any], memory: AgentMemory,
                 send_mail: Optional[Callable], group_engine: Optional[Any],
                 node_id: str, plugin_log: logging.Logger):
        self._allowed = set(config.get("actions", {}).get("allowed", ["send_mail", "log", "ignore"]))
        self._mail_recipients = config.get("actions", {}).get("mail_recipients", [])
        self._max_mail_per_hour = int(config.get("actions", {}).get("max_mail_per_hour", 10))
        self._allowed_skills = set(config.get("actions", {}).get("allowed_skills", []))
        self._max_skill_per_hour = int(config.get("actions", {}).get("max_skill_calls_per_hour", 10))
        self._memory = memory
        self._send_mail = send_mail
        self._group_engine = group_engine
        self._node_id = node_id
        self._log = plugin_log
        self._cockpit_url = "http://localhost:8080"
        self._cockpit_token = config.get("cockpit_token", "")

    async def execute(self, decision: Dict[str, Any], event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an LLM decision. Returns result dict with status."""
        action = decision.get("action", "log")
        if action not in self._allowed:
            self._log.warning(f"Action '{action}' not in allowlist, falling back to log")
            action = "log"

        try:
            if action == "ignore":
                return {"status": "ignored"}

            if action == "log":
                summary = decision.get("summary", "No summary")
                self._log.info(f"AGENT_LOG: {summary}")
                return {"status": "logged", "summary": summary}

            if action == "send_mail":
                return await self._do_send_mail(decision, event_data)

            if action == "add_group_member":
                return await self._do_add_group_member(decision)

            if action == "call_skill":
                return await self._do_call_skill(decision, event_data)

            if action == "store_note":
                return self._do_store_note(decision)

            return {"status": "unknown_action", "action": action}

        except Exception as e:
            self._log.error(f"Action execution failed: {e}")
            return {"status": "error", "error": str(e)}

    async def _do_send_mail(self, decision: Dict[str, Any],
                            event_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self._send_mail:
            return {"status": "error", "error": "send_mail not available"}

        if not self._memory.check_rate_limit("mail_out", self._max_mail_per_hour):
            self._log.warning("Mail rate limit exceeded, skipping send")
            return {"status": "rate_limited"}

        to_node = decision.get("to", "")
        from_node = event_data.get("from_node", "")

        # LLMs often truncate node IDs — resolve prefix against from_node
        if to_node and len(to_node) < 64 and from_node.startswith(to_node):
            to_node = from_node
        elif not to_node or len(to_node) != 64:
            to_node = from_node

        if not to_node or len(to_node) != 64:
            return {"status": "error", "error": f"Invalid to_node: must be 64-char hex, got '{to_node[:20]}'"}

        if self._mail_recipients and to_node not in self._mail_recipients:
            self._log.warning(f"Recipient {to_node[:16]} not in allowlist")
            return {"status": "error", "error": "Recipient not allowed"}

        msg_type = decision.get("msg_type", "text")
        body_content = decision.get("body", decision.get("summary", ""))
        session_id = decision.get("session_id", event_data.get("session_id"))

        body = {"content": body_content} if isinstance(body_content, str) else body_content

        await self._send_mail(
            to_node=to_node,
            msg_type=msg_type,
            body=body,
            session_id=session_id,
        )

        # Record outbound in conversation history
        if session_id:
            self._memory.add_conversation(
                session_id=session_id,
                from_node=self._node_id,
                direction="outbound",
                body=json.dumps(body)[:500],
            )

        self._log.info(f"AGENT_MAIL: to={to_node[:16]} type={msg_type}")
        return {"status": "sent", "to": to_node[:16]}

    async def _do_add_group_member(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        if not self._group_engine:
            return {"status": "error", "error": "group_engine not available"}

        group_id = decision.get("group_id", "")
        node_id = decision.get("node_id", "")
        if not group_id or not node_id:
            return {"status": "error", "error": "Missing group_id or node_id"}

        try:
            self._group_engine.add_member(group_id, node_id)
            self._log.info(f"AGENT_GROUP: added {node_id[:16]} to {group_id}")
            return {"status": "added", "group": group_id, "node": node_id[:16]}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _do_call_skill(self, decision: Dict[str, Any],
                             event_data: Dict[str, Any]) -> Dict[str, Any]:
        skill_name = decision.get("skill", "")
        if not skill_name:
            return {"status": "error", "error": "Missing 'skill' field"}

        if skill_name not in self._allowed_skills:
            self._log.warning(f"Skill '{skill_name}' not in agent allowlist")
            return {"status": "error", "error": f"Skill '{skill_name}' not in allowlist"}

        if not self._memory.check_rate_limit("skill_calls", self._max_skill_per_hour):
            self._log.warning("Skill call rate limit exceeded")
            return {"status": "rate_limited"}

        skill_input = decision.get("input", {})
        if isinstance(skill_input, str):
            try:
                skill_input = json.loads(skill_input)
            except (json.JSONDecodeError, TypeError):
                skill_input = {"text": skill_input}

        self._log.info(f"AGENT_SKILL: calling {skill_name}")

        # Use cockpit async execute endpoint
        import asyncio
        try:
            result = await asyncio.to_thread(
                self._cockpit_execute, skill_name, skill_input
            )
        except Exception as e:
            self._log.error(f"Skill call failed: {e}")
            return {"status": "error", "error": str(e)}

        # If there's a reply_to, send the result back
        reply_to = decision.get("reply_to", "")
        from_node = event_data.get("from_node", "")
        if reply_to and len(reply_to) < 64 and from_node.startswith(reply_to):
            reply_to = from_node
        elif not reply_to:
            reply_to = from_node

        if reply_to and len(reply_to) == 64 and self._send_mail:
            session_id = decision.get("session_id", event_data.get("session_id", ""))
            # Format result for mail
            result_summary = json.dumps(result, ensure_ascii=False)[:2000]
            body = {"content": f"Skill result ({skill_name}):\n{result_summary}"}
            try:
                await self._send_mail(
                    to_node=reply_to,
                    msg_type="text",
                    body=body,
                    session_id=session_id,
                )
                if session_id:
                    self._memory.add_conversation(
                        session_id=session_id,
                        from_node=self._node_id,
                        direction="outbound",
                        body=json.dumps(body)[:500],
                    )
                self._log.info(f"AGENT_SKILL_REPLY: {skill_name} result sent to {reply_to[:16]}")
            except Exception as e:
                self._log.error(f"Failed to send skill result: {e}")

        return {"status": "called", "skill": skill_name, "result_keys": list(result.keys()) if isinstance(result, dict) else []}

    def _cockpit_execute(self, skill_name: str, skill_input: dict) -> dict:
        """Synchronous cockpit skill execution (runs in thread)."""
        payload = json.dumps({
            "skill": skill_name,
            "input": skill_input,
            "timeout": 300,
        }).encode()

        req = Request(
            f"{self._cockpit_url}/api/execute",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._cockpit_token}",
            },
        )
        try:
            resp = urlopen(req, timeout=310)
            data = json.loads(resp.read())
            # Async endpoint returns job_id — need to poll
            job_id = data.get("job_id")
            if job_id:
                return self._poll_job(job_id)
            return data.get("result", data)
        except URLError as e:
            raise RuntimeError(f"Cockpit execute failed: {e}")

    def _poll_job(self, job_id: str, max_wait: int = 300) -> dict:
        """Poll cockpit for async job result."""
        import time as _time
        deadline = _time.time() + max_wait
        while _time.time() < deadline:
            req = Request(
                f"{self._cockpit_url}/api/jobs/{job_id}",
                headers={"Authorization": f"Bearer {self._cockpit_token}"},
            )
            try:
                resp = urlopen(req, timeout=10)
                data = json.loads(resp.read())
                status = data.get("status", "")
                if status == "completed":
                    return data.get("result", {})
                if status == "failed":
                    raise RuntimeError(f"Job failed: {data.get('error', 'unknown')}")
            except URLError:
                pass
            _time.sleep(3)
        raise RuntimeError(f"Job {job_id} timed out after {max_wait}s")

    def _do_store_note(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        key = decision.get("key", "")
        value = decision.get("value", "")
        if not key:
            return {"status": "error", "error": "Missing 'key' field"}
        if not value:
            return {"status": "error", "error": "Missing 'value' field"}

        self._memory.set_note(key, value)
        self._log.info(f"AGENT_NOTE: stored '{key}'")
        return {"status": "stored", "key": key}
