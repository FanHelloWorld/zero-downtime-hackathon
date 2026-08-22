"""The disk pool that decouples the watchdog from the supervisor agent.

The watchdog server owns chat.db and does nothing but append newly-seen messages
here. The supervisor agent server leases work out, replies, and acknowledges.
Neither process imports the other; the only contract between them is this table.

Why a leased SQLite queue rather than a JSONL file or a socket:

* **Durability.** If the supervisor crashes mid-reply, the lease expires and the
  work is picked up again instead of vanishing.
* **Restart independence.** Either side can be stopped, upgraded, or crash
  without losing messages. The watchdog keeps spooling while the agent is down.
* **Backpressure visibility.** Queue depth is a plain SQL query, so a stalled
  agent is obvious rather than silent.
* **Safe concurrency.** WAL mode plus a busy timeout lets two independent
  processes write without a lock file dance.

Rows are never deleted on acknowledgement — they move to a terminal state so the
log stays auditable. ``purge`` handles housekeeping.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import SPOOL_DB, ensure_state_dir
from .models import Message

PENDING = "pending"
LEASED = "leased"
DONE = "done"
DROPPED = "dropped"
DEAD = "dead"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS spool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_rowid       INTEGER NOT NULL UNIQUE,   -- chat.db ROWID; makes enqueue idempotent
    guid            TEXT    NOT NULL,
    chat_guid       TEXT    NOT NULL,
    chat_identifier TEXT    NOT NULL DEFAULT '',
    handle          TEXT,
    body            TEXT    NOT NULL,
    service         TEXT    NOT NULL DEFAULT 'unknown',
    style           INTEGER NOT NULL DEFAULT 0,
    display_name    TEXT,
    sent_at         TEXT    NOT NULL,
    enqueued_at     REAL    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    lease_owner     TEXT,
    lease_expires   REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS spool_state_idx ON spool (state, id);
CREATE INDEX IF NOT EXISTS spool_lease_idx ON spool (state, lease_expires);
CREATE INDEX IF NOT EXISTS spool_chat_idx  ON spool (chat_guid, id);
"""


@dataclass(frozen=True, slots=True)
class SpoolItem:
    """A spooled message plus its queue bookkeeping."""

    id: int
    attempts: int
    message: Message


@dataclass(frozen=True, slots=True)
class SpoolStats:
    pending: int = 0
    leased: int = 0
    done: int = 0
    dropped: int = 0
    dead: int = 0
    oldest_pending_age: float | None = None

    @property
    def depth(self) -> int:
        """Work still owed: queued plus in-flight."""
        return self.pending + self.leased


def _row_to_item(row: sqlite3.Row) -> SpoolItem:
    return SpoolItem(
        id=int(row["id"]),
        attempts=int(row["attempts"]),
        message=Message(
            rowid=int(row["src_rowid"]),
            guid=row["guid"],
            body=row["body"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
            handle=row["handle"],
            chat_guid=row["chat_guid"],
            chat_identifier=row["chat_identifier"] or "",
            service=row["service"] or "unknown",
            style=int(row["style"] or 0),
            display_name=row["display_name"],
        ),
    )


class Spool:
    """Disk-backed message pool shared by the two servers."""

    def __init__(self, path: Path | str = SPOOL_DB, *, busy_timeout_ms: int = 5000) -> None:
        ensure_state_dir()
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), timeout=busy_timeout_ms / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Spool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- producer side (watchdog) -----------------------------------------

    def enqueue(self, messages: list[Message]) -> int:
        """Append messages. Returns how many were newly spooled.

        Idempotent on the source ROWID, so a watchdog restart that re-reads a
        window of chat.db cannot double-queue a reply.
        """
        if not messages:
            return 0
        now = time.time()
        rows = [
            (
                m.rowid, m.guid, m.chat_guid, m.chat_identifier, m.handle, m.body,
                m.service, m.style, m.display_name, m.sent_at.isoformat(), now,
            )
            for m in messages
        ]
        cur = self._conn.executemany(
            """
            INSERT OR IGNORE INTO spool (
                src_rowid, guid, chat_guid, chat_identifier, handle, body,
                service, style, display_name, sent_at, enqueued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._conn.commit()
        return cur.rowcount

    # ---- consumer side (supervisor agent) ---------------------------------

    def lease(self, owner: str | None = None, *, limit: int = 50, lease_seconds: float = 120.0) -> list[SpoolItem]:
        """Claim up to ``limit`` items for exclusive processing.

        Picks up both never-touched rows and rows whose previous lease expired —
        that is what makes a supervisor crash recoverable rather than lossy.
        """
        owner = owner or f"{uuid.uuid4().hex[:8]}"
        now = time.time()
        expires = now + lease_seconds

        # BEGIN IMMEDIATE takes the write lock up front so two supervisors can
        # never select the same rows before either one updates them.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM spool
                WHERE state = ?
                   OR (state = ? AND lease_expires IS NOT NULL AND lease_expires < ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (PENDING, LEASED, now, limit),
            ).fetchall()

            if not rows:
                self._conn.execute("COMMIT")
                return []

            ids = [int(r["id"]) for r in rows]
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"""
                UPDATE spool
                SET state = ?, lease_owner = ?, lease_expires = ?, attempts = attempts + 1
                WHERE id IN ({placeholders})
                """,
                (LEASED, owner, expires, *ids),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        # Re-read so attempts reflects the increment we just made.
        placeholders = ",".join("?" * len(ids))
        refreshed = self._conn.execute(
            f"SELECT * FROM spool WHERE id IN ({placeholders}) ORDER BY id ASC", ids
        ).fetchall()
        return [_row_to_item(r) for r in refreshed]

    def _finish(self, ids: list[int], state: str, note: str | None) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE spool SET state = ?, lease_owner = NULL, lease_expires = NULL, "
            f"note = ? WHERE id IN ({placeholders})",
            (state, note, *ids),
        )
        self._conn.commit()

    def ack(self, ids: list[int], note: str | None = None) -> None:
        """Mark items handled. Terminal."""
        self._finish(ids, DONE, note)

    def drop(self, ids: list[int], reason: str) -> None:
        """Mark items deliberately not replied to — filtered or skipped. Terminal."""
        self._finish(ids, DROPPED, reason)

    def nack(self, ids: list[int], reason: str, *, max_attempts: int = 3) -> None:
        """Return items for another try, or dead-letter them once they've had enough.

        Without the attempt ceiling, one message that reliably crashes the agent
        would be retried until the end of time.
        """
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"""
            UPDATE spool
            SET state = CASE WHEN attempts >= ? THEN ? ELSE ? END,
                lease_owner = NULL,
                lease_expires = NULL,
                note = ?
            WHERE id IN ({placeholders})
            """,
            (max_attempts, DEAD, PENDING, reason, *ids),
        )
        self._conn.commit()

    # ---- observability -----------------------------------------------------

    def stats(self) -> SpoolStats:
        counts = {
            row["state"]: int(row["n"])
            for row in self._conn.execute("SELECT state, COUNT(*) AS n FROM spool GROUP BY state")
        }
        oldest = self._conn.execute(
            "SELECT MIN(enqueued_at) FROM spool WHERE state = ?", (PENDING,)
        ).fetchone()[0]
        return SpoolStats(
            pending=counts.get(PENDING, 0),
            leased=counts.get(LEASED, 0),
            done=counts.get(DONE, 0),
            dropped=counts.get(DROPPED, 0),
            dead=counts.get(DEAD, 0),
            oldest_pending_age=(time.time() - oldest) if oldest else None,
        )

    def dead_letters(self, limit: int = 20) -> list[SpoolItem]:
        rows = self._conn.execute(
            "SELECT * FROM spool WHERE state = ? ORDER BY id DESC LIMIT ?", (DEAD, limit)
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def requeue_dead(self) -> int:
        cur = self._conn.execute(
            "UPDATE spool SET state = ?, attempts = 0, note = NULL WHERE state = ?",
            (PENDING, DEAD),
        )
        self._conn.commit()
        return cur.rowcount

    def purge(self, older_than_seconds: float = 7 * 24 * 3600) -> int:
        """Delete long-settled rows so the pool doesn't grow without bound."""
        cutoff = time.time() - older_than_seconds
        cur = self._conn.execute(
            "DELETE FROM spool WHERE state IN (?, ?) AND enqueued_at < ?",
            (DONE, DROPPED, cutoff),
        )
        self._conn.commit()
        return cur.rowcount
