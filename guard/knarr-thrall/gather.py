"""Thrall Switchboard — Context gatherer stage.

Pipeline stage between [filter] and [evaluate]. Two gather modes:

1. **Catalog fields** (v3.7) — Recipe declares `gather = ["field1", "field2"]`.
   Engine looks up fields in the catalog, deduplicates by source, fetches once
   per source, extracts and formats individual fields. Supports glob syntax
   (e.g. `gather = ["economy.*"]`).

2. **Legacy [[gather]] blocks** — Recipe declares `[[gather]]` sections with
   explicit source/endpoint. Still supported for backward compatibility.

Sources (catalog):
    status   — GET /api/status (peer_count, skill_count, uptime, node_version)
    economy  — GET /api/economy (net_position, worst_position, settlement_candidates, ...)
    wallet   — internal wallet.get_status() (daily_spend, budget_remaining, budget_ceiling)
    journal  — internal db query (recent_actions, action_counts)
    peers    — GET /api/peers (peer_list, skill_inventory, peer_skill_gaps)
    probe    — computed, no API call (probe_entropy, probe_unique_peers)

Sources (legacy):
    cockpit  — HTTP GET to cockpit API
    memory   — Query thrall structured memory
    static   — Literal values
"""

import fnmatch
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("thrall.gather")

# ── Catalog ──────────────────────────────────────────────────────────────

_catalog: Optional[Dict[str, dict]] = None


def load_catalog(plugin_dir: str) -> Dict[str, dict]:
    """Load gather-field-catalog.toml and return {field_name: metadata}."""
    global _catalog
    if _catalog is not None:
        return _catalog

    path = os.path.join(plugin_dir, "gather-field-catalog.toml")
    if not os.path.exists(path):
        logger.warning(f"CATALOG not found: {path}")
        _catalog = {}
        return _catalog

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    fields = raw.get("fields", {})
    # Validate: every field must have 'cost'
    valid = {}
    for name, meta in fields.items():
        if "cost" not in meta:
            logger.warning(f"CATALOG field '{name}' missing 'cost', skipping")
            continue
        valid[name] = meta

    _catalog = valid
    logger.info(f"CATALOG loaded: {len(valid)} fields from {path}")
    return _catalog


def _expand_field_names(names: List[str], catalog: Dict[str, dict]) -> List[str]:
    """Expand glob patterns (e.g. 'economy.*') against catalog field names.

    Fields are matched by their cost attribute for source-glob patterns,
    or by fnmatch against field names for wildcard patterns.
    """
    expanded = []
    for name in names:
        if "*" in name or "?" in name:
            # Try matching against field names first
            matched = [f for f in catalog if fnmatch.fnmatch(f, name)]
            # Also try source-based glob: "economy.*" matches all fields with cost="economy"
            if not matched and "." in name:
                source_prefix = name.split(".")[0]
                matched = [f for f in catalog
                           if catalog[f].get("cost") == source_prefix]
            if not matched:
                logger.warning(f"GATHER glob '{name}' matched 0 fields")
            expanded.extend(matched)
        else:
            expanded.append(name)
    # Deduplicate preserving order
    seen = set()
    result = []
    for f in expanded:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


# ── Source fetchers ──────────────────────────────────────────────────────

def _fetch_source_status(commerce) -> dict:
    """GET /api/status — returns raw JSON."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce._get("/api/status") or {}


def _fetch_source_economy(commerce) -> dict:
    """GET /api/economy — returns raw JSON."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce.get_economy() or {}


def _fetch_source_wallet(wallet) -> dict:
    """Internal wallet.get_status()."""
    if not wallet:
        return {"error": "no wallet module"}
    return wallet.get_status()


def _fetch_source_peers(commerce) -> dict:
    """GET /api/peers — returns raw JSON."""
    if not commerce:
        return {"error": "no commerce module"}
    return commerce._get("/api/peers") or {}


def _fetch_source_journal(db, hours: int = 24) -> dict:
    """Query thrall_journal for recent actions + action counts."""
    if not db:
        return {"error": "no db"}
    cutoff = time.time() - (hours * 3600)

    # Recent actions (last 5)
    rows = db.query_journal(limit=5, since=cutoff)
    recent = []
    for r in rows:
        recent.append({
            "timestamp": r.get("timestamp", 0),
            "recipe": r.get("pipeline", ""),
            "action": r.get("action_name", ""),
            "outcome": r.get("eval_type", ""),
        })

    # Action counts by type
    stats = db.get_stats(since=cutoff)
    counts = {}
    for s in stats:
        action = s.get("action_name", "unknown")
        counts[action] = counts.get(action, 0) + s.get("count", 0)

    return {"recent_actions": recent, "action_counts": counts}


def _fetch_source_memory(db, hours: int = 72) -> dict:
    """Query thrall_memory table for settlement and skill call history."""
    if not db:
        return {"error": "no db"}
    cutoff = time.time() - (hours * 3600)

    settlements = db.query_memory(skill="settlement-review", limit=10, since=cutoff)
    return {"recent_settlements": settlements}


def _fetch_source_probe(db, hours: int = 24) -> dict:
    """Computed probe fields — no API call."""
    if not db:
        return {"probe_entropy": 0.0, "probe_unique_peers": 0}
    cutoff = time.time() - (hours * 3600)
    rows = db.query_journal(limit=500, since=cutoff)

    # Action distribution for entropy
    action_counts: Dict[str, int] = {}
    unique_peers: set = set()
    for r in rows:
        action = r.get("action_name", "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
        from_node = r.get("from_node", "")
        if from_node:
            unique_peers.add(from_node[:16])

    entropy = _shannon_entropy(action_counts)
    return {
        "probe_entropy": round(entropy, 3),
        "probe_unique_peers": len(unique_peers),
    }


def _shannon_entropy(counts: Dict[str, int]) -> float:
    """Shannon entropy of a count distribution."""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for c in counts.values():
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)
    return entropy


# ── Field extraction and formatting ─────────────────────────────────────

def _extract_field(field_name: str, field_meta: dict,
                   source_data: dict) -> str:
    """Extract and format a single field from its source response.

    Returns a formatted string suitable for LLM context.
    """
    fmt = field_meta.get("format", "scalar")
    field_type = field_meta.get("type", "text")
    source = field_meta.get("cost", "")

    # Probe fields are direct keys in the source dict
    if source == "none":
        val = source_data.get(field_name)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Journal fields
    if source == "journal":
        if field_name == "recent_actions":
            actions = source_data.get("recent_actions", [])
            if not actions:
                return "no recent actions"
            lines = []
            for a in actions:
                lines.append(f"  {a.get('recipe', '?')}: {a.get('action', '?')} ({a.get('outcome', '?')})")
            return "\n".join(lines)
        elif field_name == "action_counts":
            counts = source_data.get("action_counts", {})
            if not counts:
                return "no actions in last 24h"
            lines = [f"  {k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
            return "\n".join(lines)
        return str(source_data.get(field_name, "unavailable"))

    # Wallet fields — direct keys from get_status()
    if source == "wallet":
        key_map = {
            "daily_spend": "daily_spent",
            "budget_remaining": "remaining",
            "budget_ceiling": "ceiling",
        }
        key = key_map.get(field_name, field_name)
        val = source_data.get(key)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Status fields — direct keys
    if source == "status":
        key_map = {
            "peer_count": "peer_count",
            "skill_count": "skill_count",
            "uptime": "uptime",
            "node_version": "version",
        }
        key = key_map.get(field_name, field_name)
        val = source_data.get(key)
        if val is not None:
            return _format_value(val, fmt, field_type)
        return "unavailable"

    # Economy fields — need specific extraction logic
    if source == "economy":
        return _extract_economy_field(field_name, field_meta, source_data)

    # Peers fields
    if source == "peers":
        return _extract_peers_field(field_name, field_meta, source_data)

    # Memory fields
    if source == "memory":
        return _extract_memory_field(field_name, field_meta, source_data)

    return "unavailable"


def _extract_economy_field(field_name: str, field_meta: dict,
                           data: dict) -> str:
    """Extract economy fields from /api/economy response."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    # The economy API returns various structures depending on knarr version.
    # Common patterns: {positions: [...], summary: {...}}
    positions = data.get("positions", data.get("ledger", []))
    if isinstance(positions, dict):
        positions = list(positions.values()) if positions else []

    if field_name == "net_position":
        total = sum(float(p.get("balance", 0)) for p in positions) if positions else 0
        return f"{total:.2f}"

    elif field_name == "worst_position":
        if not positions:
            return "no positions"
        worst = min(positions, key=lambda p: float(p.get("balance", 0)))
        pk = worst.get("peer_public_key", worst.get("peer_id", "?"))[:16]
        bal = float(worst.get("balance", 0))
        return f"{pk}: {bal:.2f}"

    elif field_name == "settlement_candidates":
        # Peers where utilization is high (>80%)
        candidates = []
        for p in positions:
            bal = float(p.get("balance", 0))
            # Approximate utilization (negative balance = we owe them)
            if bal < -5.0:  # rough threshold
                pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
                candidates.append(f"  {pk}: balance={bal:.2f}")
        return "\n".join(candidates) if candidates else "no settlement candidates"

    elif field_name == "credit_floor_peers":
        floor_peers = []
        for p in positions:
            bal = float(p.get("balance", 0))
            if bal < -10.0:  # near hard limit
                pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
                floor_peers.append(f"  {pk}: balance={bal:.2f}")
        return "\n".join(floor_peers) if floor_peers else "no peers at credit floor"

    elif field_name == "bilateral_summary":
        if not positions:
            return "no bilateral positions"
        lines = []
        for p in positions:
            pk = p.get("peer_public_key", p.get("peer_id", "?"))[:16]
            bal = float(p.get("balance", 0))
            prov = p.get("tasks_provided", p.get("calls_provided", 0))
            cons = p.get("tasks_consumed", p.get("calls_consumed", 0))
            lines.append(f"  {pk}: bal={bal:.2f} prov={prov} cons={cons}")
        return "\n".join(lines)

    elif field_name in ("top_skills_by_revenue", "top_skills_by_cost"):
        # These need receipt/skill economics data which may not be in /api/economy
        return "unavailable (requires skill economics data)"

    return "unavailable"


def _extract_peers_field(field_name: str, field_meta: dict,
                         data: dict) -> str:
    """Extract peers fields from /api/peers response."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    peers = data if isinstance(data, list) else data.get("peers", [])

    if field_name == "peer_list":
        if not peers:
            return "no connected peers"
        lines = []
        for p in peers:
            pk = p.get("node_id", p.get("peer_id", "?"))[:16]
            host = p.get("host", "?")
            skills = p.get("skill_count", 0)
            lines.append(f"  {pk}: {host} skills={skills}")
        return "\n".join(lines)

    elif field_name == "skill_inventory":
        lines = []
        for p in peers:
            pk = p.get("node_id", p.get("peer_id", "?"))[:16]
            skills = p.get("skills", [])
            if skills:
                for s in skills[:5]:  # cap at 5 per peer
                    name = s if isinstance(s, str) else s.get("name", "?")
                    lines.append(f"  {pk}: {name}")
        return "\n".join(lines) if lines else "no skills discovered"

    elif field_name == "peer_skill_gaps":
        # Would require comparing our skills vs network skills — simplified
        return "unavailable (requires skill comparison)"

    return "unavailable"


def _extract_memory_field(field_name: str, field_meta: dict,
                           data: dict) -> str:
    """Extract memory fields from thrall_memory query results."""
    if isinstance(data, dict) and "error" in data:
        return f"error: {data['error']}"

    if field_name == "recent_settlements":
        entries = data.get("recent_settlements", [])
        if not entries:
            return "no recent settlements"
        lines = []
        for e in entries:
            ts = time.strftime("%m-%d %H:%M", time.gmtime(e.get("timestamp", 0)))
            peer = e.get("node_id", "?")[:16]
            outcome = e.get("outcome", "?")
            amount = e.get("amount", 0)
            reason = e.get("reasoning", "")[:60]
            lines.append(f"  {ts} {peer}: {outcome} {amount:.1f}cr — {reason}")
        return "\n".join(lines)

    return "unavailable"


def _format_value(val: Any, fmt: str, field_type: str) -> str:
    """Format a value according to catalog format spec."""
    if val is None:
        return "unavailable"

    if "rendered as hours" in fmt:
        try:
            return f"{float(val) / 3600:.1f}h"
        except (ValueError, TypeError):
            return str(val)

    if "2 decimal" in fmt:
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)

    if "3 decimal" in fmt:
        try:
            return f"{float(val):.3f}"
        except (ValueError, TypeError):
            return str(val)

    return str(val)


# ── Main gatherer class ─────────────────────────────────────────────────

class ContextGatherer:
    """Fetches contextual data for recipe evaluation.

    Two modes:
    - gather_fields(["field1", "field2"]) — catalog-based (v3.7)
    - gather(recipe, envelope) — legacy [[gather]] blocks
    """

    def __init__(self, commerce=None, memory=None, db=None,
                 wallet=None, plugin_dir=None):
        self._commerce = commerce
        self._memory = memory
        self._db = db
        self._wallet = wallet
        self._plugin_dir = plugin_dir
        self._catalog = None

    @property
    def catalog(self) -> Dict[str, dict]:
        if self._catalog is None:
            if self._plugin_dir:
                self._catalog = load_catalog(self._plugin_dir)
            else:
                self._catalog = {}
        return self._catalog

    def set_commerce(self, commerce):
        self._commerce = commerce

    def set_memory(self, memory):
        self._memory = memory

    def set_wallet(self, wallet):
        self._wallet = wallet

    def set_db(self, db):
        self._db = db

    # ── Catalog-based gather (v3.7) ─────────────────────────────────

    def gather_fields(self, field_names: List[str]) -> Dict[str, str]:
        """Fetch fields by catalog name. Deduplicates by source.

        Returns {field_name: formatted_string}.
        """
        cat = self.catalog
        if not cat:
            logger.warning("GATHER_FIELDS no catalog loaded")
            return {}

        # Expand globs
        expanded = _expand_field_names(field_names, cat)
        if not expanded:
            return {}

        # Group by source (cost attribute) for dedup
        by_source: Dict[str, List[str]] = {}
        for name in expanded:
            meta = cat.get(name)
            if not meta:
                logger.warning(f"GATHER_FIELDS unknown field: {name}")
                continue
            source = meta["cost"]
            by_source.setdefault(source, []).append(name)

        # Fetch each source once
        source_data: Dict[str, dict] = {}
        t0 = time.time()

        for source, fields in by_source.items():
            try:
                if source == "status":
                    source_data[source] = _fetch_source_status(self._commerce)
                elif source == "economy":
                    source_data[source] = _fetch_source_economy(self._commerce)
                elif source == "wallet":
                    source_data[source] = _fetch_source_wallet(self._wallet)
                elif source == "journal":
                    source_data[source] = _fetch_source_journal(self._db)
                elif source == "peers":
                    source_data[source] = _fetch_source_peers(self._commerce)
                elif source == "memory":
                    source_data[source] = _fetch_source_memory(self._db)
                elif source == "none":
                    source_data[source] = _fetch_source_probe(self._db)
                else:
                    logger.warning(f"GATHER_FIELDS unknown source: {source}")
                    source_data[source] = {"error": f"unknown source: {source}"}
            except Exception as e:
                logger.warning(f"GATHER_FIELDS source {source} failed: {e}")
                source_data[source] = {"error": str(e)}

        # Extract individual fields
        results = {}
        for name in expanded:
            meta = cat.get(name)
            if not meta:
                continue
            source = meta["cost"]
            data = source_data.get(source, {})
            try:
                results[name] = _extract_field(name, meta, data)
            except Exception as e:
                logger.warning(f"GATHER_FIELDS extract {name} failed: {e}")
                results[name] = f"error: {e}"

        wall_ms = int((time.time() - t0) * 1000)
        sources_hit = len(source_data)
        logger.debug(
            f"GATHER_FIELDS {len(results)} fields from {sources_hit} sources, "
            f"{wall_ms}ms")
        return results

    # ── Legacy [[gather]] blocks ────────────────────────────────────

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
            return self._commerce._get(endpoint)

    def _fetch_memory(self, cfg: dict) -> Any:
        """Query thrall structured memory."""
        if not self._memory:
            return {"error": "no memory module"}

        query = cfg.get("query", {})

        if cfg.get("format") == "prompt":
            return self._memory.format_for_prompt(
                node_id=query.get("node_id") or None,
                skill=query.get("skill") or None,
                limit=int(query.get("limit", "5")))

        if cfg.get("format") == "summary":
            node_id = query.get("node_id")
            if not node_id:
                return {"error": "summary requires node_id"}
            return self._memory.get_peer_summary(
                node_id=node_id,
                skill=query.get("skill") or None,
                days=int(query.get("days", "7")))

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
