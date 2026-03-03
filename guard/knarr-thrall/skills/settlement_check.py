"""settlement-check-lite — Autonomous bilateral position checker.

Queries the cockpit ledger, finds positions above soft_threshold,
builds signed netting proposals using thrall's delegated identity,
and submits settlement requests via mail.

Triggered by the settlement-check recipe (on_tick, hotwire, hourly).
No LLM inference — pure computation.

Input:
  - threshold: utilization threshold (default: 0.8)
  - cockpit_url: override cockpit URL (default from plugin.toml)

Output:
  - status: ok/no_positions/disabled/ceiling_hit/error
  - positions_checked: number of ledger entries examined
  - settlements_proposed: number of proposals sent
  - total_amount: sum of proposed settlement amounts
  - wall_ms: total execution time
"""

import json
import os
import sys
import time

NODE = None


def set_node(node):
    global NODE
    NODE = node


def _get_plugin_dir():
    """Resolve thrall plugin directory from skill file location."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins", "06-thrall",
    )


def _get_config():
    """Read thrall config from plugin.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    plugin_toml = os.path.join(_get_plugin_dir(), "plugin.toml")
    with open(plugin_toml, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("config", {})


async def handle(input_data: dict) -> dict:
    t0 = time.time()
    threshold = float(input_data.get("threshold", "0.8"))

    # Load config
    try:
        config = _get_config()
    except Exception as e:
        return {"status": "error", "result_summary": f"Config load failed: {e}",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    thrall_cfg = config.get("thrall", {})
    identity_cfg = thrall_cfg.get("identity", {})
    wallet_cfg = thrall_cfg.get("wallet", {})

    # Check if identity is enabled
    if not identity_cfg.get("enabled", False):
        return {"status": "disabled",
                "result_summary": "Thrall identity not enabled",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    # Import thrall modules
    plugin_dir = _get_plugin_dir()
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    from identity import ThrallIdentity
    from wallet import ThrallWallet
    from commerce import ThrallCommerce
    from db import ThrallDB

    # Initialize components
    db = ThrallDB(os.path.join(plugin_dir, "thrall.db"))
    identity = ThrallIdentity(plugin_dir, identity_cfg)
    wallet = ThrallWallet(db, wallet_cfg)
    cockpit_url = thrall_cfg.get("cockpit_url", "http://127.0.0.1:8080")
    cockpit_token = thrall_cfg.get("cockpit_token", "")

    if not identity.enabled:
        return {"status": "disabled",
                "result_summary": "Thrall identity failed to initialize",
                "wall_ms": str(int((time.time() - t0) * 1000))}

    # Get node_id from NODE if available
    node_id = NODE.node_info.node_id if NODE else ""

    # Policy defaults (from knarr.toml, fallback to sensible defaults)
    policy = config.get("policy", {})
    commerce = ThrallCommerce(
        cockpit_url=cockpit_url,
        cockpit_token=cockpit_token,
        node_id=node_id,
        default_policy=policy,
    )

    # Check positions
    ledger = commerce.query_ledger()
    positions_checked = len(ledger)
    positions = commerce.check_positions(threshold)

    if not positions:
        return {
            "status": "no_positions",
            "positions_checked": str(positions_checked),
            "settlements_proposed": "0",
            "total_amount": "0",
            "result_summary": f"No positions above {threshold * 100:.0f}% threshold",
            "wall_ms": str(int((time.time() - t0) * 1000)),
        }

    # Process each over-threshold position
    proposed = 0
    total_amount = 0.0
    skipped_ceiling = 0

    for pos in positions:
        amount = pos["settle_amount"]

        # Check wallet ceiling
        if not wallet.can_spend(amount):
            skipped_ceiling += 1
            continue

        # Build and sign netting document
        doc = commerce.build_netting_doc(
            peer_pk=pos["peer_public_key"],
            settle_amount=amount,
            current_balance=pos["balance"],
            target_balance=pos["target_balance"],
            identity=identity,
        )

        # Submit via mail (need send_mail function)
        # For now, log the proposal — actual submission requires send_mail_fn
        # which is wired through the ActionExecutor in handler.py
        if NODE and hasattr(NODE, "send_mail"):
            body = {
                "type": "knarr/commerce/settle_request",
                "proposal": json.loads(doc),
            }
            try:
                await NODE.send_mail(
                    pos["peer_public_key"], "knarr/commerce/settle_request", body)
                wallet.record_spend(amount, f"netting:{pos['peer_public_key'][:16]}",
                                    pos["peer_public_key"],
                                    f"Auto-settlement at {pos['utilization_pct']}% utilization")
                proposed += 1
                total_amount += amount
            except Exception as e:
                pass  # logged by commerce module
        else:
            # No NODE — record the proposal anyway for observability
            wallet.record_spend(amount, f"netting:{pos['peer_public_key'][:16]}",
                                pos["peer_public_key"],
                                f"Proposal built (no send_mail), {pos['utilization_pct']}% util")
            proposed += 1
            total_amount += amount

    status = "ok"
    if proposed == 0 and skipped_ceiling > 0:
        status = "ceiling_hit"

    summary_parts = [f"checked={positions_checked}", f"proposed={proposed}"]
    if skipped_ceiling:
        summary_parts.append(f"ceiling_blocked={skipped_ceiling}")

    return {
        "status": status,
        "positions_checked": str(positions_checked),
        "settlements_proposed": str(proposed),
        "total_amount": f"{total_amount:.1f}",
        "skipped_ceiling": str(skipped_ceiling),
        "result_summary": ", ".join(summary_parts),
        "wall_ms": str(int((time.time() - t0) * 1000)),
    }
