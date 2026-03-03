"""Thrall Settlement Identity — Commerce wrappers.

Thin wrappers around cockpit API endpoints that give thrall structured
access to the credit/settlement system. Uses the same HTTP pattern as
handler.py:_cockpit_execute().

v3.3.1: Receipt queries use PluginContext.query_receipts() (D-051) when
available, falling back to cockpit HTTP. Ledger/economy queries still
use cockpit HTTP (no PluginContext equivalent exists).

Methods:
    query_ledger()      → GET /api/ledger
    get_economy()       → GET /api/economy
    query_receipt(ref)  → ctx.query_receipts() or GET /api/receipts/{ref}
    check_positions()   → computed from ledger
    build_netting_doc() → signed by ThrallIdentity (eddsa-jcs-2022)
    submit_settlement() → send settle_request mail to peer
"""

import json
import logging
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("thrall.commerce")


class ThrallCommerce:
    """Cockpit API wrappers for credit system access."""

    def __init__(self, cockpit_url: str, cockpit_token: str,
                 node_id: str = "", default_policy: dict = None,
                 query_receipts_fn: Callable = None):
        self._url = cockpit_url
        self._token = cockpit_token
        self._node_id = node_id
        self._query_receipts_fn = query_receipts_fn
        # Default credit policy for threshold calculation
        self._initial_credit = float((default_policy or {}).get("initial_credit", 100))
        self._min_balance = float((default_policy or {}).get("min_balance", -50))
        self._ssl_ctx = None
        if self._url.startswith("https"):
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _get(self, path: str, timeout: int = 10) -> Any:
        """GET request to cockpit API."""
        req = Request(
            f"{self._url}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            resp = urlopen(req, timeout=timeout, context=self._ssl_ctx)
            return json.loads(resp.read())
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            logger.error(f"COMMERCE_API_ERROR {path}: {e} body={body}")
            return {"error": str(e)}
        except (URLError, Exception) as e:
            logger.error(f"COMMERCE_API_ERROR {path}: {e}")
            return {"error": str(e)}

    def query_ledger(self) -> List[dict]:
        """Fetch all bilateral positions from cockpit."""
        result = self._get("/api/ledger")
        if isinstance(result, list):
            logger.debug(f"LEDGER_QUERY positions={len(result)}")
            return result
        if isinstance(result, dict) and "error" in result:
            return []
        return []

    def get_economy(self) -> dict:
        """Fetch aggregated economy summary."""
        return self._get("/api/economy")

    def query_receipt(self, reference: str) -> Optional[dict]:
        """Fetch credit note by job_id reference.

        Uses PluginContext.query_receipts() when available (v0.35.0+),
        falls back to cockpit HTTP.
        """
        if self._query_receipts_fn:
            try:
                results = self._query_receipts_fn(
                    document_type=None, counterparty=None,
                    since=None, limit=50)
                for r in results:
                    if r.get("order_ref") == reference:
                        return r
            except Exception as e:
                logger.debug(f"query_receipts_fn failed, falling back to HTTP: {e}")

        result = self._get(f"/api/receipts/{reference}")
        if isinstance(result, dict) and "error" not in result:
            return result
        return None

    def check_positions(self, threshold: float = 0.8) -> List[dict]:
        """Find bilateral positions above utilization threshold.

        Mirrors the logic in knarr/commerce/netting.py:run_netting_cycle().
        Returns list of dicts with peer info, balance, utilization, settle_amount.
        """
        entries = self.query_ledger()
        over_threshold = []

        for entry in entries:
            pk = entry.get("peer_public_key", "")
            balance = float(entry.get("balance", 0))

            # Use default policy for threshold calculation
            ic = self._initial_credit
            mb = self._min_balance
            credit_range = ic - mb
            if credit_range <= 0:
                continue

            utilization = (ic - balance) / credit_range
            if utilization < threshold:
                continue

            # Calculate settlement to reach 50% utilization
            soft_target = 0.5
            target_balance = ic - (soft_target * credit_range)
            settle_amount = target_balance - balance

            if settle_amount < 10.0:  # min_settlement_amount
                continue

            over_threshold.append({
                "peer_public_key": pk,
                "balance": round(balance, 2),
                "utilization_pct": round(utilization * 100, 1),
                "settle_amount": round(settle_amount, 2),
                "target_balance": round(target_balance, 2),
            })

        if over_threshold:
            logger.info(f"POSITION_CHECK found={len(over_threshold)} "
                        f"above {threshold * 100:.0f}% threshold")
        return over_threshold

    def build_netting_doc(self, peer_pk: str, settle_amount: float,
                          current_balance: float, target_balance: float,
                          identity) -> dict:
        """Build a signed netting proposal document (eddsa-jcs-2022).

        Args:
            peer_pk: Peer's public key hex.
            settle_amount: Credits to settle.
            current_balance: Current bilateral balance.
            target_balance: Target balance after settlement.
            identity: ThrallIdentity instance for signing.

        Returns:
            Signed dict with embedded proof object.
        """
        payload = {
            "type": "thrall_netting_proposal",
            "version": 1,
            "proposer_node_id": self._node_id,
            "thrall_public_key": identity.public_key_hex,
            "peer_public_key": peer_pk,
            "current_balance": current_balance,
            "settle_amount": settle_amount,
            "target_balance": target_balance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        signed = identity.sign_document(payload)
        logger.info(f"NETTING_DOC peer={peer_pk[:16]} amount={settle_amount:.1f}")
        return signed

    def submit_settlement(self, peer_pk: str, doc: dict,
                          send_mail_fn: Callable) -> bool:
        """Submit settlement proposal via knarr-mail.

        Sends as msg_type 'knarr/commerce/settle_request' which the
        peer's commerce handler (handlers.py:handle_settle_request)
        already processes.
        """
        try:
            body = {
                "type": "knarr/commerce/settle_request",
                "proposal": doc,
            }
            send_mail_fn(peer_pk, "knarr/commerce/settle_request", body)
            logger.info(f"SETTLEMENT_SUBMITTED peer={peer_pk[:16]}")
            return True
        except Exception as e:
            logger.error(f"SETTLEMENT_SUBMIT_FAILED peer={peer_pk[:16]}: {e}")
            return False
