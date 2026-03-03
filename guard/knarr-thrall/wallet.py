"""Thrall Settlement Identity — Scoped wallet with operator ceiling.

Petty-cash mechanism: thrall can authorize credit operations up to
the daily ceiling without human approval. Resets at midnight UTC.

Config:
    [config.thrall.wallet]
    ceiling = 50.0          # max credits per day
"""

import calendar
import logging
import time
from datetime import datetime, timezone

from db import ThrallDB

logger = logging.getLogger("thrall.wallet")


class ThrallWallet:
    """Daily-capped spending authority for thrall settlement operations."""

    def __init__(self, db: ThrallDB, config: dict):
        self._db = db
        self._ceiling = float(config.get("ceiling", 50.0))
        self._enabled = config.get("enabled", True) and self._ceiling > 0
        if self._enabled:
            status = self.get_status()
            logger.info(
                f"WALLET_INIT ceiling={self._ceiling} "
                f"daily_spent={status['daily_spent']:.1f} "
                f"remaining={status['remaining']:.1f}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _day_start(self) -> float:
        """Midnight UTC today as unix timestamp."""
        now = datetime.now(timezone.utc)
        return calendar.timegm(now.replace(
            hour=0, minute=0, second=0, microsecond=0).timetuple())

    def can_spend(self, amount: float) -> bool:
        """Check if amount fits within remaining daily ceiling."""
        if not self._enabled:
            return False
        spent = self._db.get_daily_spend(self._day_start())
        return (spent + amount) <= self._ceiling

    def record_spend(self, amount: float, reference: str = "",
                     peer_pk: str = "", description: str = ""):
        """Record a spending event. Call after successful settlement proposal."""
        self._db.record_wallet_spend(amount, reference, peer_pk, description)
        logger.info(
            f"WALLET_SPEND amount={amount:.1f} ref={reference[:16]} "
            f"peer={peer_pk[:16]}")

    def get_status(self) -> dict:
        """Current wallet status."""
        spent = self._db.get_daily_spend(self._day_start())
        return {
            "ceiling": self._ceiling,
            "daily_spent": round(spent, 2),
            "remaining": round(max(0, self._ceiling - spent), 2),
            "day_start": self._day_start(),
        }
