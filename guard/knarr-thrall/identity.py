"""Thrall Settlement Identity — Delegated Ed25519 keypair.

Gives thrall its own signing key, separate from the node's identity.
Peers can distinguish "thrall signed this" from "the operator signed this."

The 32-byte seed is stored in a keyfile inside the plugin directory.
Config-gated: only initializes if [config.thrall.identity] enabled = true.
"""

import base64
import json
import logging
import os
import secrets
from datetime import datetime, timezone

logger = logging.getLogger("thrall.identity")


class ThrallIdentity:
    """Delegated Ed25519 identity for autonomous thrall operations."""

    def __init__(self, plugin_dir: str, config: dict):
        self._plugin_dir = plugin_dir
        self._enabled = config.get("enabled", False)
        self._signing_key = None
        self._public_key_hex = ""

        if not self._enabled:
            logger.info("IDENTITY_DISABLED — no delegated keypair")
            return

        keyfile = config.get("keyfile", "thrall_identity.key")
        self._keyfile_path = os.path.join(plugin_dir, keyfile)
        self._load_or_generate()

    def _load_or_generate(self):
        """Load existing keypair or generate a new one."""
        from nacl.signing import SigningKey

        if os.path.exists(self._keyfile_path):
            with open(self._keyfile_path, "rb") as f:
                seed = f.read()
            if len(seed) != 32:
                logger.error(f"IDENTITY_ERROR keyfile corrupt ({len(seed)} bytes, expected 32)")
                return
            self._signing_key = SigningKey(seed)
            logger.info(f"IDENTITY_LOADED public_key={self.public_key_hex[:16]}...")
        else:
            seed = secrets.token_bytes(32)
            with open(self._keyfile_path, "wb") as f:
                f.write(seed)
            self._signing_key = SigningKey(seed)
            logger.info(f"IDENTITY_GENERATED public_key={self.public_key_hex[:16]}...")

    @property
    def enabled(self) -> bool:
        return self._enabled and self._signing_key is not None

    @property
    def public_key_hex(self) -> str:
        if not self._signing_key:
            return ""
        if not self._public_key_hex:
            self._public_key_hex = self._signing_key.verify_key.encode().hex()
        return self._public_key_hex

    def sign_document(self, payload: dict) -> str:
        """Sign a document using canonical JSON + Ed25519.

        Follows the same pattern as knarr/commerce/receipts.py:create_credit_note().
        Returns canonical JSON string with 'signature' field appended.

        Raises RuntimeError if identity is disabled.
        """
        if not self._signing_key:
            raise RuntimeError("Thrall identity not initialized")

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        raw_sig = self._signing_key.sign(canonical).signature
        sig_b64 = base64.b64encode(raw_sig).decode("ascii")

        payload["signature"] = sig_b64
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def revoke(self):
        """Delete the keyfile — operator escape hatch."""
        if os.path.exists(self._keyfile_path):
            os.remove(self._keyfile_path)
            logger.warning("IDENTITY_REVOKED — keyfile deleted")
        self._signing_key = None
        self._public_key_hex = ""
        self._enabled = False


def verify_thrall_signature(doc_json: str) -> bool:
    """Verify a thrall-signed document.

    Expects 'thrall_public_key' and 'signature' fields in the JSON.
    Returns True if signature is valid, False otherwise.
    """
    try:
        from nacl.signing import VerifyKey

        doc = json.loads(doc_json)
        sig_b64 = doc.get("signature")
        pk_hex = doc.get("thrall_public_key")
        if not sig_b64 or not pk_hex:
            return False

        sig = base64.b64decode(sig_b64)
        payload = {k: v for k, v in doc.items() if k != "signature"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        vk = VerifyKey(bytes.fromhex(pk_hex))
        vk.verify(canonical, sig)
        return True
    except Exception:
        return False
