"""Knarr Skill Base Class — L1 compliance by default.

Providers write one method: `async def run(self, input_data) -> dict`.
Everything else is inherited: healthcheck, structured logging, vault
integration, error reporting, input validation, execution timing.

Usage (leaf skill):

    from skill_base import SkillBase

    class MySkill(SkillBase):
        name = "my-skill-lite"

        async def run(self, data):
            return {"result": "hello"}

    # Module-level exports — must be local functions (not bound methods)
    # so inspect.getmodule(handle) returns THIS module for set_node discovery.
    _skill = MySkill()

    def set_node(node):
        _skill.set_node(node)

    async def handle(input_data: dict) -> dict:
        return await _skill.handle(input_data)

Usage (chain skill):

    class MyChain(SkillBase):
        name = "my-chain-lite"
        chain = ["sub-skill-a", "sub-skill-b"]

        async def run(self, data):
            a = await self.call("sub-skill-a", {"input": data["x"]})
            b = await self.call("sub-skill-b", {"input": data["y"]})
            return {"a_result": a["result"], "b_result": b["result"]}

    _skill = MyChain()

    def set_node(node):
        _skill.set_node(node)

    async def handle(input_data: dict) -> dict:
        return await _skill.handle(input_data)

Usage (leaf skill with healthcheck probe):

    class OllamaSkill(SkillBase):
        name = "my-ollama-skill"

        async def healthcheck(self):
            # Override to probe your specific dependency
            resp = requests.get("http://localhost:11434/api/tags", timeout=5)
            resp.raise_for_status()

        async def run(self, data):
            ...
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional

from _common import ensure_flat_str_dict, error_result, truncate_text


class SkillBase:
    """Base class for knarr skills. Inherit and implement `run()`."""

    # --- Override these in subclass ---
    name: str = "unnamed-skill"
    chain: List[str] = []               # sub-skills this orchestrator depends on
    required_fields: List[str] = []     # input fields that must be present
    call_local_timeout: int = 600_000   # default timeout for call_local (ms)

    def __init__(self):
        self._node: Any = None

    # -- Node injection (called by serve_batch1.py) --

    def set_node(self, node: Any) -> None:
        self._node = node

    @property
    def node(self) -> Any:
        if self._node is None:
            raise RuntimeError(f"{self.name}: node not initialized (set_node not called)")
        return self._node

    # -- Public entry point (registered as handler) --

    async def handle(self, input_data: dict) -> dict:
        """Main entry point. Do not override — override run() instead."""

        # Healthcheck fast path
        if input_data.get("_healthcheck"):
            return await self._do_healthcheck()

        # Input validation
        err = self._validate_input(input_data)
        if err:
            return err

        # Execute with timing and error handling
        start = time.time()
        try:
            result = await self.run(input_data)
        except Exception as exc:
            wall_ms = int((time.time() - start) * 1000)
            return self._error(exc, wall_ms)

        wall_ms = int((time.time() - start) * 1000)

        # Ensure flat str dict and inject timing
        if isinstance(result, dict):
            result["_wall_ms"] = str(wall_ms)
            result["_skill"] = self.name
            return ensure_flat_str_dict(result)
        return result

    # -- Override these --

    async def run(self, input_data: dict) -> dict:
        """Implement your skill logic here. Return a dict."""
        raise NotImplementedError(f"{self.name}: run() not implemented")

    async def healthcheck(self) -> None:
        """Override to probe skill-specific dependencies.

        Raise an exception if unhealthy. Return normally if healthy.
        For chain skills, the base class automatically pings all sub-skills
        listed in `chain` — you only need to override this for extra checks
        (e.g. verifying an external API or model is reachable).
        """
        pass

    # -- Chain helpers --

    async def call(self, skill_name: str, input_data: dict,
                   timeout_ms: Optional[int] = None) -> dict:
        """Call a sub-skill via NODE.call_local. Convenience wrapper."""
        return await self.node.call_local(
            skill_name, input_data,
            timeout_ms=timeout_ms or self.call_local_timeout,
        )

    async def check_chain(self) -> Optional[str]:
        """Pre-exec healthcheck: ping all chain dependencies.

        Returns None if healthy, error string if any link is down.
        """
        if not self.chain:
            return None

        for skill in self.chain:
            try:
                result = await self.call(skill, {"_healthcheck": True}, timeout_ms=15_000)
                if result.get("error"):
                    return f"{skill}: {result['error']}"
            except Exception as exc:
                return f"{skill}: {exc}"
        return None

    # -- Internal --

    async def _do_healthcheck(self) -> dict:
        """Handle _healthcheck request."""
        start = time.time()
        try:
            # Check chain dependencies first
            chain_err = await self.check_chain()
            if chain_err:
                return {"status": "unhealthy", "error": chain_err, "skill": self.name}

            # Run skill-specific health probe
            await self.healthcheck()

            wall_ms = int((time.time() - start) * 1000)
            resp: Dict[str, str] = {
                "status": "ok",
                "skill": self.name,
                "latency_ms": str(wall_ms),
            }
            if self.chain:
                resp["chain"] = ",".join(self.chain)
                resp["chain_status"] = "warm"
            return resp

        except Exception as exc:
            wall_ms = int((time.time() - start) * 1000)
            return {
                "status": "unhealthy",
                "skill": self.name,
                "error": truncate_text(str(exc), 500),
                "latency_ms": str(wall_ms),
            }

    def _validate_input(self, input_data: dict) -> Optional[dict]:
        """Validate required fields. Returns error_result or None."""
        if not self.required_fields:
            return None
        missing = [f for f in self.required_fields if not input_data.get(f)]
        if missing:
            return error_result(f"Missing required field(s): {', '.join(missing)}")
        return None

    def _error(self, exc: Exception, wall_ms: int) -> dict:
        """Format an exception as a structured error response."""
        # Don't leak full tracebacks to consumers
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            msg = f"{self.name}: {exc}"
        else:
            msg = f"{self.name} error: {type(exc).__name__}: {exc}"
        return error_result(msg)
