"""Thrall Switchboard — Context gatherer stage.

New pipeline stage between [filter] and [evaluate]. Reads [[gather]]
sections from recipe TOML, fetches data from various sources, and injects
results into the envelope as gather.{name} fields.

This is the "event-based research" pattern: instead of just reacting to
an event, thrall gathers contextual data before making a decision.

Sources:
    cockpit  — HTTP GET to cockpit API (economy, ledger, receipts, positions)
    memory   — Query thrall structured memory (peer history, outcomes)
    static   — Literal values (for template defaults)

Recipe config example:
    [[gather]]
    name = "positions"
    source = "cockpit"
    endpoint = "/api/positions"
    threshold = "0.6"

    [[gather]]
    name = "peer_history"
    source = "memory"
    format = "prompt"
    query = { node_id = "{{peer_pk}}", skill = "settlement-review", limit = "5" }
"""

import logging
import re
import time
from typing import Any, Dict

logger = logging.getLogger("thrall.gather")


class ContextGatherer:
    """Fetches contextual data for recipe evaluation."""

    def __init__(self, commerce=None, memory=None):
        self._commerce = commerce  # ThrallCommerce instance
        self._memory = memory      # ThrallMemory instance

    def set_commerce(self, commerce):
        self._commerce = commerce

    def set_memory(self, memory):
        self._memory = memory

    async def gather(self, recipe: dict, envelope) -> Dict[str, Any]:
        """Run all [[gather]] sections and return results dict.

        Results are keyed by the gather name. Each value is the fetched
        data (dict, list, or string depending on source).
        """
        gather_configs = recipe.get("gather", [])
        if not gather_configs:
            return {}

        results = {}
        t0 = time.time()

        for cfg in gather_configs:
            name = cfg.get("name", "")
            source = cfg.get("source", "")
            if not name or not source:
                continue

            try:
                value = self._fetch_one(cfg, envelope)
                results[name] = value
                preview = str(value)[:100] if value else "empty"
                logger.debug(f"GATHER {name} ({source}): {preview}")
            except Exception as e:
                logger.warning(f"GATHER {name} failed: {e}")
                results[name] = {"error": str(e)}

        wall_ms = int((time.time() - t0) * 1000)
        if results:
            logger.debug(f"GATHER total: {len(results)} sources, {wall_ms}ms")
        return results

    def _fetch_one(self, cfg: dict, envelope) -> Any:
        """Fetch data from a single source."""
        source = cfg["source"]

        # Substitute {{field}} placeholders with envelope values
        resolved = _resolve_templates(cfg, envelope)

        if source == "cockpit":
            return self._fetch_cockpit(resolved)
        elif source == "memory":
            return self._fetch_memory(resolved)
        elif source == "static":
            return resolved.get("value", "")
        else:
            raise ValueError(f"Unknown gather source: {source}")

    def _fetch_cockpit(self, cfg: dict) -> Any:
        """Fetch data from cockpit API."""
        if not self._commerce:
            return {"error": "no commerce module"}

        endpoint = cfg.get("endpoint", "")
        if not endpoint:
            return {"error": "no endpoint specified"}

        if endpoint == "/api/economy":
            return self._commerce.get_economy()
        elif endpoint == "/api/ledger":
            return self._commerce.query_ledger()
        elif endpoint == "/api/positions":
            threshold = float(cfg.get("threshold", "0.8"))
            return self._commerce.check_positions(threshold)
        elif endpoint.startswith("/api/receipts/"):
            ref = endpoint.split("/")[-1]
            return self._commerce.query_receipt(ref)
        else:
            # Generic GET for future endpoints
            return self._commerce._get(endpoint)

    def _fetch_memory(self, cfg: dict) -> Any:
        """Query thrall structured memory."""
        if not self._memory:
            return {"error": "no memory module"}

        query = cfg.get("query", {})

        # format=prompt returns LLM-ready text
        if cfg.get("format") == "prompt":
            return self._memory.format_for_prompt(
                node_id=query.get("node_id") or None,
                skill=query.get("skill") or None,
                limit=int(query.get("limit", "5")))

        # format=summary returns aggregated stats
        if cfg.get("format") == "summary":
            node_id = query.get("node_id")
            if not node_id:
                return {"error": "summary requires node_id"}
            return self._memory.get_peer_summary(
                node_id=node_id,
                skill=query.get("skill") or None,
                days=int(query.get("days", "7")))

        # Default: raw records
        return self._memory.query(
            node_id=query.get("node_id") or None,
            skill=query.get("skill") or None,
            outcome=query.get("outcome") or None,
            limit=int(query.get("limit", "10")),
            include_dryrun=query.get("include_dryrun") == "true")


def _resolve_templates(cfg: dict, envelope) -> dict:
    """Substitute {{field}} placeholders with envelope values."""
    resolved = {}
    for k, v in cfg.items():
        if isinstance(v, str) and "{{" in v:
            resolved[k] = re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: envelope.get(m.group(1).strip(), m.group(0)),
                v)
        elif isinstance(v, dict):
            resolved[k] = _resolve_templates(v, envelope)
        else:
            resolved[k] = v
    return resolved
