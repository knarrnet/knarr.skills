"""Fleet database — tracks provisioned nodes."""

import sqlite3
import time
import logging
from pathlib import Path

logger = logging.getLogger("fenrir.fleet_db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_nodes (
    label TEXT PRIMARY KEY,
    server_id TEXT,
    provider TEXT,
    location TEXT,
    ip TEXT,
    node_id TEXT,
    encryption_key TEXT,
    seed_encrypted TEXT,
    nonce TEXT,
    status TEXT DEFAULT 'provisioning',
    created_at REAL,
    last_seen REAL,
    report_received_at REAL
)
"""


class FleetDB:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()
        logger.info(f"Fleet DB initialized at {db_path}")

    def insert(self, label: str, server_id: str, provider: str, location: str,
               ip: str, node_id: str, encryption_key: str, seed_encrypted: str,
               nonce: str) -> None:
        self._conn.execute(
            """INSERT INTO fleet_nodes
               (label, server_id, provider, location, ip, node_id,
                encryption_key, seed_encrypted, nonce, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', ?)""",
            (label, server_id, provider, location, ip, node_id,
             encryption_key, seed_encrypted, nonce, time.time()),
        )
        self._conn.commit()

    def update_status(self, label: str, status: str) -> None:
        self._conn.execute(
            "UPDATE fleet_nodes SET status = ? WHERE label = ?",
            (status, label),
        )
        self._conn.commit()

    def update_report(self, label: str, report_time: float) -> None:
        self._conn.execute(
            "UPDATE fleet_nodes SET report_received_at = ?, status = 'online', last_seen = ? WHERE label = ?",
            (report_time, report_time, label),
        )
        self._conn.commit()

    def get_by_label(self, label: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM fleet_nodes WHERE label = ?", (label,)
        ).fetchone()
        return dict(row) if row else None

    def get_by_nonce(self, nonce: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM fleet_nodes WHERE nonce = ?", (nonce,)
        ).fetchone()
        return dict(row) if row else None

    def get_next_docker_ip(self, subnet: str = "172.21.0") -> str:
        """Return next free IP in {subnet}.10 - {subnet}.249 for Docker nodes."""
        rows = self._conn.execute(
            "SELECT ip FROM fleet_nodes WHERE provider = 'docker' AND status != 'destroyed'"
        ).fetchall()
        used = {r["ip"] for r in rows}
        for i in range(10, 250):
            candidate = f"{subnet}.{i}"
            if candidate not in used:
                return candidate
        raise RuntimeError(f"No free IPs in {subnet}.10-249 ({len(used)} in use)")

    def list_all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM fleet_nodes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
