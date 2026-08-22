"""The event log, indexed — the same records as ``events.jsonl``, queryable.

``events.jsonl`` is not going anywhere: it is append-only, greppable, and the
first thing to reach for when the system misbehaves. What it cannot do is answer
"what happened in the last minute", "how many times did we stay quiet today", or
"give me everything after id 4,182" without re-reading the whole file. A UI needs
all three, several times a second.

So every event is written twice. This half is a SQLite table whose autoincrement
``id`` doubles as a stream cursor: a reader asks for ``id > n``, gets what it
missed, and remembers the last id. That works across processes, which matters —
the two servers emit, and a third one reads.

Two rules hold here:

* **A connection per call.** ``emit`` is called from loop threads, worker
  threads, notifier threads and exception handlers in both servers. Opening a
  fresh connection each time costs about a millisecond on a local file at the
  handful-per-second rate this runs at, and it sidesteps every SQLite
  thread-affinity bug by construction. The existing per-call file open sets the
  precedent.
* **Failure is swallowed.** An event store that breaks must never break the
  pipeline that is writing to it — least of all inside the error handlers that
  emit events about other failures. A failed insert degrades to JSONL-only and
  warns once.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import EVENTS_DB, ensure_state_dir

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,   -- also the stream cursor
    ts     REAL NOT NULL,
    iso    TEXT NOT NULL,
    stage  TEXT NOT NULL,
    event  TEXT NOT NULL,
    chat   TEXT,                                -- already anonymised by events.anon
    detail TEXT NOT NULL DEFAULT '{}'           -- JSON
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts);
CREATE INDEX IF NOT EXISTS events_stage_idx ON events (stage, id);
CREATE INDEX IF NOT EXISTS events_chat_idx ON events (chat, id);
"""

BUSY_TIMEOUT_MS = 5000

_ready: set[str] = set()
_ready_lock = threading.Lock()
_warned = False


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection, creating the schema the first time this process sees it."""
    key = str(path)
    conn = sqlite3.connect(key, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    with _ready_lock:
        fresh = key not in _ready
        if fresh:
            _ready.add(key)
    if fresh:
        ensure_state_dir()
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


def _warn_once(exc: Exception) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"[plug] event store unavailable, falling back to the JSONL log only: {exc}",
        file=sys.stderr,
        flush=True,
    )


def record(
    stage: str,
    event: str,
    *,
    chat: str | None,
    detail: dict[str, Any],
    ts: float | None = None,
    iso: str = "",
    path: Path | str | None = None,
) -> int | None:
    """Append one event. Returns its id, or None if the store could not take it.

    Never raises. The caller is in the middle of doing something more important.

    ``path`` resolves at call time rather than as a default argument, so tests
    can redirect the whole store by patching ``EVENTS_DB`` on this module. A
    default evaluated at import would bind the real path permanently and quietly
    fill the user's dashboard with synthetic events.
    """
    try:
        conn = _connect(EVENTS_DB if path is None else path)

        try:
            cursor = conn.execute(
                "INSERT INTO events (ts, iso, stage, event, chat, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time() if ts is None else ts,
                    iso,
                    stage,
                    event,
                    chat,
                    json.dumps(detail, default=str),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()
    except Exception as exc:  # a broken log must not break the pipeline
        _warn_once(exc)
        return None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        detail = json.loads(row["detail"])
    except (ValueError, TypeError):
        detail = {}
    return {
        "id": int(row["id"]),
        "ts": float(row["ts"]),
        "iso": row["iso"],
        "stage": row["stage"],
        "event": row["event"],
        "chat": row["chat"],
        "detail": detail,
    }


class EventStore:
    """Read side. One connection per thread — open your own, never share."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._conn = _connect(EVENTS_DB if path is None else path)


    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- streaming --------------------------------------------------------

    def max_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM events").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def since(self, cursor: int, limit: int = 200) -> list[dict[str, Any]]:
        """Everything after ``cursor``, oldest first — the order it happened in."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (cursor, limit)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---- reading ----------------------------------------------------------

    def recent(
        self, limit: int = 100, *, stage: str | None = None, chat: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest first, for a log panel."""
        clauses, params = [], []
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        if chat:
            clauses.append("chat = ?")
            params.append(chat)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def counts(self, since_seconds: float = 72 * 3600) -> dict[str, int]:
        """``stage/event`` tallies over a window. One query, not a file scan."""
        cutoff = time.time() - since_seconds
        rows = self._conn.execute(
            "SELECT stage, event, COUNT(*) AS n FROM events WHERE ts > ? "
            "GROUP BY stage, event",
            (cutoff,),
        ).fetchall()
        return {f"{r['stage']}/{r['event']}": int(r["n"]) for r in rows}

    def histogram(self, since_seconds: float = 72 * 3600, buckets: int = 72) -> list[int]:
        """Event counts per equal time bucket, oldest first — the quiet meter."""
        now = time.time()
        start = now - since_seconds
        width = since_seconds / max(1, buckets)
        rows = self._conn.execute(
            "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, COUNT(*) AS n "
            "FROM events WHERE ts > ? GROUP BY b",
            (start, width, start),
        ).fetchall()
        out = [0] * buckets
        for row in rows:
            index = int(row["b"])
            if 0 <= index < buckets:
                out[index] = int(row["n"])
        return out

    def chats(self, limit: int = 50) -> list[str]:
        rows = self._conn.execute(
            "SELECT chat, MAX(id) AS m FROM events WHERE chat IS NOT NULL AND chat != '-' "
            "GROUP BY chat ORDER BY m DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["chat"] for r in rows]

    # ---- housekeeping -----------------------------------------------------

    def purge(self, older_than_seconds: float = 7 * 24 * 3600) -> int:
        cutoff = time.time() - older_than_seconds
        cursor = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount
