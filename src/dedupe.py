"""SQLite-backed dedupe for `X-GitHub-Delivery` IDs.

GitHub retries webhooks. Without dedupe we could double-commit or double-reply
to the same review comment.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RETENTION_SECONDS = 14 * 24 * 60 * 60  # two weeks


class DeliveryStore:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL
                )
                """
            )

    def claim(self, delivery_id: str) -> bool:
        """Atomically record this delivery ID. Returns True if it was new."""
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO deliveries (delivery_id, received_at) VALUES (?, ?)",
                    (delivery_id, now),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "DELETE FROM deliveries WHERE received_at < ?",
                (now - RETENTION_SECONDS,),
            )
            return True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
