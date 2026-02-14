"""Knarr MCP Server — native access to the knarr network from Claude Code.

Wraps the cockpit API. Mail (send/poll/ack), skill execution (any skill,
local or remote), and peer discovery. Runs as stdio MCP server.

Config (in .mcp.json or settings):
{
  "mcpServers": {
    "knarr-mail": {
      "command": "py",
      "args": ["-3.13", "F:\\knarr_agents\\prod\\knarr-batch1-provider\\mcp\\knarr_mail_mcp.py"],
      "env": {
        "COCKPIT_URL": "http://localhost:8080/api/execute",
        "COCKPIT_TOKEN": "your-cockpit-token"
      }
    }
  }
}
"""

import json
import os
import urllib.request
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("knarr-mail")

COCKPIT_URL = os.environ.get("COCKPIT_URL", "http://localhost:8080/api/execute")
COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
PEERS_URL = COCKPIT_URL.replace("/api/execute", "/api/peers")

# Known nodes for delivery routing (add your frequently-contacted peers here)
KNOWN_NODES = {
    # "node_id_hex": {"host": "1.2.3.4", "port": 9100, "name": "friendly-name"},
}


def _cockpit(skill: str, input_data: dict, provider: dict = None) -> dict:
    """Call a skill via cockpit API."""
    payload = {"skill": skill, "input": input_data}
    if provider:
        payload["provider"] = provider
    req = urllib.request.Request(
        COCKPIT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {COCKPIT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _resolve_provider(node_id: str) -> Optional[dict]:
    """Look up delivery address for a node."""
    if node_id in KNOWN_NODES:
        info = KNOWN_NODES[node_id]
        return {"node_id": node_id, "host": info["host"], "port": info["port"]}
    # Try peers API
    try:
        req = urllib.request.Request(
            PEERS_URL,
            headers={"Authorization": f"Bearer {COCKPIT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            peers = json.loads(resp.read())
        for peer in peers:
            if peer.get("node_id") == node_id:
                return {"node_id": node_id, "host": peer["host"], "port": peer["port"]}
    except Exception:
        pass
    return None


@mcp.tool
def send_mail(
    to: str,
    content: str,
    message_type: str = "text",
    session: str = "",
) -> str:
    """Send a knarr-mail message to another node on the network.

    Args:
        to: Destination node ID (64-char hex string)
        content: Message content (text or structured data)
        message_type: Message type — text, offer, delivery, ack, error
        session: Optional session ID for conversation threading
    """
    body = {"type": message_type, "content": content}
    if session:
        body["session"] = session

    provider = _resolve_provider(to)
    result = _cockpit("knarr-mail", {
        "action": "send",
        "to": to,
        "body": body,
    }, provider=provider)

    out = result.get("output_data", {})
    status = out.get("status", "unknown")
    msg_id = out.get("message_id", "")
    wall = result.get("wall_time_ms", "?")

    if status == "delivered":
        return f"Delivered ({wall}ms). Message ID: {msg_id}"
    else:
        return f"Send failed: {json.dumps(out)}"


@mcp.tool
def poll_mail(
    status_filter: str = "unread",
    limit: int = 20,
) -> str:
    """Poll the knarr-mail inbox for messages.

    Args:
        status_filter: Filter by status — unread, read, or all
        limit: Maximum number of messages to return (1-50)
    """
    input_data = {
        "action": "poll",
        "limit": min(limit, 50),
    }
    if status_filter != "all":
        input_data["filters"] = {"status": status_filter}

    result = _cockpit("knarr-mail", input_data)
    out = result.get("output_data", {})
    messages = out.get("messages", [])

    if not messages:
        return "Inbox empty (no matching messages)."

    lines = [f"**{len(messages)} message(s):**\n"]
    for msg in messages:
        sender = msg["from"][:16] + "..."
        body = msg.get("body", {})
        ts = msg.get("timestamp", 0)
        sid = body.get("session") or msg.get("session_id", "")
        content = body.get("content", "(no content)")

        # Truncate long content for readability
        preview = content[:300] + "..." if len(content) > 300 else content

        lines.append(
            f"- **From**: {sender} | **Type**: {body.get('type', '?')} | "
            f"**Session**: {sid or 'none'}\n"
            f"  **ID**: {msg['message_id']}\n"
            f"  {preview}\n"
        )

    return "\n".join(lines)


@mcp.tool
def ack_mail(
    message_ids: str,
    disposition: str = "read",
) -> str:
    """Acknowledge knarr-mail messages as read or archived.

    Args:
        message_ids: Comma-separated message IDs to acknowledge
        disposition: How to mark them — read or archived
    """
    ids = [mid.strip() for mid in message_ids.split(",") if mid.strip()]
    if not ids:
        return "No message IDs provided."

    result = _cockpit("knarr-mail", {
        "action": "ack",
        "message_ids": ids,
        "disposition": disposition,
    })

    out = result.get("output_data", {})
    acked = out.get("acknowledged", 0)
    return f"Acknowledged {acked} message(s) as {disposition}."


@mcp.tool
def list_peers() -> str:
    """List known peers on the knarr network with their node IDs."""
    try:
        req = urllib.request.Request(
            PEERS_URL,
            headers={"Authorization": f"Bearer {COCKPIT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            peers = json.loads(resp.read())
    except Exception as e:
        return f"Failed to get peers: {e}"

    if not peers:
        return "No peers found."

    # Deduplicate by node_id (keep first seen)
    seen = {}
    for p in peers:
        nid = p.get("node_id", "?")
        if nid not in seen:
            seen[nid] = p

    lines = [f"**{len(seen)} peer(s):**\n"]
    for nid, p in seen.items():
        host = p.get("host", "?")
        port = p.get("port", "?")
        name = KNOWN_NODES.get(nid, {}).get("name", "")
        label = f" ({name})" if name else ""
        lines.append(f"- `{nid[:16]}...`{label} — {host}:{port}")

    return "\n".join(lines)


@mcp.tool
def call_skill(
    skill: str,
    input_json: str,
    provider_node: str = "",
) -> str:
    """Execute any skill on the knarr network — local or remote.

    This is the universal gateway: any skill registered on our node or
    discoverable via DHT can be called. For remote skills, provide the
    target node ID.

    Args:
        skill: Skill name (e.g. "digest-voice-lite", "llm-toolcall-lite", "web-search")
        input_json: JSON object with skill input parameters
        provider_node: Optional target node ID for remote execution. If empty, calls local.
    """
    try:
        input_data = json.loads(input_json)
        if not isinstance(input_data, dict):
            return "Error: input_json must be a JSON object"
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON — {e}"

    provider = None
    if provider_node:
        provider = _resolve_provider(provider_node)
        if not provider:
            return f"Error: could not resolve node {provider_node[:16]}... — not in peers"

    try:
        result = _cockpit(skill, input_data, provider=provider)
    except Exception as e:
        return f"Error: {e}"

    status = result.get("status", "unknown")
    output = result.get("output_data", {})
    wall = result.get("wall_time_ms", "?")
    error = result.get("error", {})

    if error and error.get("message"):
        return f"Error ({wall}ms): {error['message']}"

    # Format output — truncate large values for readability
    lines = [f"**Status**: {status} ({wall}ms)\n"]
    for k, v in output.items():
        sv = str(v)
        if len(sv) > 500:
            sv = sv[:500] + f"... ({len(sv)} chars total)"
        lines.append(f"**{k}**: {sv}")

    return "\n".join(lines)


@mcp.tool
def list_skills(query: str = "") -> str:
    """List skills available on the local node. Optionally filter by keyword.

    Args:
        query: Optional keyword to filter skills by name or tag
    """
    try:
        req = urllib.request.Request(
            COCKPIT_URL.replace("/api/execute", "/api/skills"),
            headers={"Authorization": f"Bearer {COCKPIT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        return f"Failed to get skills: {e}"

    # API returns {"local": [...], "network": [...]}
    local_skills = raw.get("local", []) if isinstance(raw, dict) else raw
    network_skills = raw.get("network", []) if isinstance(raw, dict) else []

    lines = []

    # Local skills
    filtered_local = local_skills
    if query:
        q = query.lower()
        filtered_local = [s for s in local_skills if q in s.get("name", "").lower()
                          or q in str(s.get("tags", [])).lower()
                          or q in s.get("description", "").lower()]

    if filtered_local:
        lines.append(f"**Local ({len(filtered_local)}):**\n")
        for s in filtered_local[:30]:
            name = s.get("name", "?")
            vis = s.get("visibility", "?")
            desc = s.get("description", "")[:80]
            lines.append(f"- **{name}** ({vis}) — {desc}" if desc else f"- **{name}** ({vis})")
        if len(filtered_local) > 30:
            lines.append(f"  ... and {len(filtered_local) - 30} more")

    # Network skills
    filtered_net = network_skills
    if query:
        q = query.lower()
        filtered_net = [s for s in network_skills if q in s.get("name", "").lower()
                        or q in str(s.get("tags", [])).lower()
                        or q in s.get("description", "").lower()]

    if filtered_net:
        lines.append(f"\n**Network ({len(filtered_net)}):**\n")
        for s in filtered_net[:20]:
            name = s.get("name", "?")
            desc = s.get("description", "")[:80]
            providers = s.get("providers", [])
            nodes = ", ".join(p.get("node_id", "?")[:8] + "..." for p in providers[:3])
            lines.append(f"- **{name}** [{nodes}] — {desc}")

    if not lines:
        return f"No skills found{' matching ' + repr(query) if query else ''}."

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
