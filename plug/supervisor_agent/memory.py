"""Supervisor-owned state: conversation history, send log, rate counters.

Lives in its own SQLite file (``~/.plug/supervisor.db``). The watchdog never
reads or writes it, and the chat.db cursor is not here — that belongs to the
watchdog server.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from plug.config import SUPERVISOR_DB, ensure_state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_guid TEXT NOT NULL,
    role      TEXT NOT NULL,          -- 'user' | 'assistant'
    content   TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS history_chat_idx ON history (chat_guid, id);

CREATE TABLE IF NOT EXISTS sends (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_guid TEXT NOT NULL,
    ts        REAL NOT NULL,
    dry_run   INTEGER NOT NULL DEFAULT 0,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sends_chat_ts_idx ON sends (chat_guid, ts);
CREATE INDEX IF NOT EXISTS sends_ts_idx ON sends (ts);

CREATE TABLE IF NOT EXISTS plans (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_guid TEXT NOT NULL,
    ts        REAL NOT NULL,
    payload   TEXT NOT NULL          -- the plan, as JSON
);
CREATE INDEX IF NOT EXISTS plans_chat_idx ON plans (chat_guid, id);
"""


class Memory:
    def __init__(self, path: Path | str = SUPERVISOR_DB) -> None:
        ensure_state_dir()
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- conversation history --------------------------------------------

    def append_history(self, chat_guid: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO history (chat_guid, role, content, ts) VALUES (?, ?, ?, ?)",
            (chat_guid, role, content, time.time()),
        )
        self._conn.commit()

    def recent_history(self, chat_guid: str, turns: int) -> list[dict[str, str]]:
        """Last ``turns`` messages for a chat, oldest first, in Messages API shape."""
        rows = self._conn.execute(
            "SELECT role, content FROM history WHERE chat_guid = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_guid, turns),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---- behind-the-scenes plans -----------------------------------------

    def save_plan(self, chat_guid: str, payload: str) -> None:
        """Append a plan. Kept append-only so /plan can show the read evolving."""
        self._conn.execute(
            "INSERT INTO plans (chat_guid, ts, payload) VALUES (?, ?, ?)",
            (chat_guid, time.time(), payload),
        )
        self._conn.commit()

    def latest_plan(self, chat_guid: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM plans WHERE chat_guid = ? ORDER BY id DESC LIMIT 1",
            (chat_guid,),
        ).fetchone()
        return row["payload"] if row else None

    def recent_plans(self, chat_guid: str, limit: int = 10) -> list[tuple[float, str]]:
        rows = self._conn.execute(
            "SELECT ts, payload FROM plans WHERE chat_guid = ? ORDER BY id DESC LIMIT ?",
            (chat_guid, limit),
        ).fetchall()
        return [(float(r["ts"]), r["payload"]) for r in rows]

    def chats_with_plans(self, limit: int = 50) -> list[str]:
        rows = self._conn.execute(
            "SELECT chat_guid, MAX(id) AS m FROM plans GROUP BY chat_guid ORDER BY m DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["chat_guid"] for r in rows]

    # ---- send log and rate counters --------------------------------------

    def record_send(self, chat_guid: str, text: str, dry_run: bool) -> None:
        self._conn.execute(
            "INSERT INTO sends (chat_guid, ts, dry_run, text) VALUES (?, ?, ?, ?)",
            (chat_guid, time.time(), int(dry_run), text),
        )
        self._conn.commit()

    def sends_in_last_hour(self, chat_guid: str | None = None) -> int:
        """Count real (non-dry-run) sends in the trailing hour."""
        cutoff = time.time() - 3600
        if chat_guid is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sends WHERE ts > ? AND dry_run = 0", (cutoff,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sends WHERE ts > ? AND dry_run = 0 AND chat_guid = ?",
                (cutoff, chat_guid),
            ).fetchone()
        return int(row[0])

    def last_send_ts(self, chat_guid: str) -> float | None:
        row = self._conn.execute(
            "SELECT MAX(ts) FROM sends WHERE chat_guid = ? AND dry_run = 0",
            (chat_guid,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
