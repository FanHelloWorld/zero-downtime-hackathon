"""Watchdog-private state: just the chat.db cursor.

Kept in its own SQLite file so the watchdog has no read or write dependency on
supervisor state. Either server can be wiped and rebuilt independently.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from plug.config import WATCHDOG_DB, ensure_state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CURSOR_KEY = "cursor"


class WatchdogState:
    def __init__(self, path: Path | str = WATCHDOG_DB) -> None:
        ensure_state_dir()
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WatchdogState":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_cursor(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)
        ).fetchone()
        return int(row["value"]) if row else None

    def set_cursor(self, rowid: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CURSOR_KEY, str(rowid)),
        )
        self._conn.commit()

    def seed_cursor(self, current_max_rowid: int) -> int:
        """Cold-start guard.

        On a first run there is no cursor, and chat.db holds ~156k historical
        messages. Starting from zero would spool your entire message history for
        reply. Seed at the current high-water mark so only new traffic is pooled.
        """
        existing = self.get_cursor()
        if existing is not None:
            return existing
        self.set_cursor(current_max_rowid)
        return current_max_rowid
