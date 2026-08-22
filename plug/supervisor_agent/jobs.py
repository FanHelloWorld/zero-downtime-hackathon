"""Background work the supervisor promised and has not delivered yet.

A leased queue, the same shape as ``plug/spool.py`` and for the same reason: the
moment the agent says "hold on, let me look something up", the chat is owed an
answer, and an answer owed cannot live in a thread's local variables. If the
process dies mid-lookup, the job is still on disk with its chat, its objective
and how many times it has been tried.

Why its own file rather than a table in ``supervisor.db``: worker threads and the
loop thread write this concurrently. That wants WAL and a busy timeout, which the
spool has and ``Memory`` — opened thread-bound with the default rollback journal
— does not.

Lifecycle::

    queued ──claim──▶ running ──ready──▶ ready ──deliver──▶ delivered
                         │                  │
                         ├─fail─▶ queued    └─blocked─▶ blocked   (safety said no)
                         │        └─▶ failed  (out of attempts)
                         └─expire─▶ expired   (took too long)

Terminal rows are kept, not deleted, so "what did it do and why did it stop" is
answerable after the fact.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from plug.config import JOBS_DB, ensure_state_dir

QUEUED = "queued"
RUNNING = "running"
READY = "ready"
DELIVERED = "delivered"
FAILED = "failed"
EXPIRED = "expired"
BLOCKED = "blocked"

TERMINAL = (DELIVERED, FAILED, EXPIRED, BLOCKED)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT NOT NULL UNIQUE,   -- stable public id, safe to log
    chat_guid     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    objective     TEXT NOT NULL,
    context       TEXT NOT NULL DEFAULT '',   -- dossier brief, snapshotted at dispatch
    pillar        TEXT NOT NULL DEFAULT '',
    is_group      INTEGER NOT NULL DEFAULT 0,
    handle        TEXT,                       -- for the send fallback strategies
    service       TEXT NOT NULL DEFAULT 'iMessage',
    created_at    REAL NOT NULL,
    started_at    REAL,                       -- reset on every claim
    state         TEXT NOT NULL DEFAULT 'queued',

    lease_owner   TEXT,
    lease_expires REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    findings      TEXT,                       -- what the worker found
    reply         TEXT,                       -- the composed follow-up
    note          TEXT,
    settled_at    REAL
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state);
CREATE INDEX IF NOT EXISTS jobs_chat_idx ON jobs (chat_guid, created_at);
"""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    job_key: str
    chat_guid: str
    kind: str
    objective: str
    context: str
    pillar: str
    is_group: bool
    handle: str | None
    service: str
    created_at: float
    started_at: float | None
    state: str
    attempts: int

    findings: str
    reply: str
    note: str

    @property
    def age(self) -> float:
        """How long the chat has been waiting. Measured from the promise."""
        return time.time() - self.created_at

    @property
    def running_for(self) -> float:
        """How long this attempt has been going. 0 when not running."""
        return 0.0 if self.started_at is None else time.time() - self.started_at



@dataclass(frozen=True, slots=True)
class JobStats:
    queued: int = 0
    running: int = 0
    ready: int = 0
    delivered: int = 0
    failed: int = 0
    expired: int = 0
    blocked: int = 0

    @property
    def in_flight(self) -> int:
        return self.queued + self.running + self.ready


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        job_key=row["job_key"],
        chat_guid=row["chat_guid"],
        kind=row["kind"],
        objective=row["objective"],
        context=row["context"] or "",
        pillar=row["pillar"] or "",
        is_group=bool(row["is_group"]),
        handle=row["handle"],
        service=row["service"] or "iMessage",
        created_at=float(row["created_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        state=row["state"],

        attempts=int(row["attempts"]),
        findings=row["findings"] or "",
        reply=row["reply"] or "",
        note=row["note"] or "",
    )


class JobStore:
    """The queue. One connection per thread — open your own, never share."""

    def __init__(self, path: Path | str = JOBS_DB, *, busy_timeout_ms: int = 5000) -> None:
        ensure_state_dir()
        self._conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a table an earlier version already created.

        ``CREATE TABLE IF NOT EXISTS`` cannot do this, and a bare ``ALTER`` in
        SCHEMA fails on the second start — the same idiom as ``Spool._migrate``.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if existing and "started_at" not in existing:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")


    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- dispatch ---------------------------------------------------------

    def enqueue(
        self,
        chat_guid: str,
        kind: str,
        objective: str,
        *,
        context: str = "",
        pillar: str = "",
        is_group: bool = False,
        handle: str | None = None,
        service: str = "iMessage",
    ) -> Job:
        job_key = "j_" + secrets.token_hex(5)
        cursor = self._conn.execute(
            "INSERT INTO jobs (job_key, chat_guid, kind, objective, context, pillar, "
            "is_group, handle, service, created_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_key, chat_guid, kind, objective, context, pillar,
                int(is_group), handle, service, time.time(), QUEUED,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_job(row)

    # ---- the worker side --------------------------------------------------

    def claim(self, owner: str, *, limit: int = 1, lease_seconds: float = 150.0) -> list[Job]:
        """Take up to ``limit`` jobs to run. Also retakes lapsed leases.

        ``BEGIN IMMEDIATE`` up front, like the spool: two threads reaching here
        at once must not both claim the same job and run it twice, which would
        mean paying for the research twice and texting the chat twice.
        """
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = ? OR (state = ? AND lease_expires < ?) "
                "ORDER BY id LIMIT ?",
                (QUEUED, RUNNING, now, limit),
            ).fetchall()
            if not rows:
                self._conn.rollback()
                return []

            ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE jobs SET state = ?, lease_owner = ?, lease_expires = ?, "
                f"started_at = ?, attempts = attempts + 1 WHERE id IN ({marks})",
                (RUNNING, owner, now + lease_seconds, now, *ids),
            )

            fresh = self._conn.execute(
                f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY id", ids
            ).fetchall()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return [_row_to_job(r) for r in fresh]

    def ready(self, job_id: int, findings: str, reply: str) -> None:
        """The worker finished. The follow-up is written but not yet sent."""
        self._conn.execute(
            "UPDATE jobs SET state = ?, findings = ?, reply = ?, "
            "lease_owner = NULL, lease_expires = NULL WHERE id = ?",
            (READY, findings, reply, job_id),
        )
        self._conn.commit()

    def fail(self, job_id: int, reason: str, *, max_attempts: int = 2) -> str:
        """Return the job for another try, or give up on it.

        One UPDATE so the decision and the write cannot disagree — the same
        trick ``Spool.nack`` uses.
        """
        self._conn.execute(
            "UPDATE jobs SET state = CASE WHEN attempts >= ? THEN ? ELSE ? END, "
            "note = ?, lease_owner = NULL, lease_expires = NULL, "
            "settled_at = CASE WHEN attempts >= ? THEN ? ELSE NULL END WHERE id = ?",
            (max_attempts, FAILED, QUEUED, reason, max_attempts, time.time(), job_id),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["state"] if row else FAILED

    # ---- the loop side ----------------------------------------------------

    def note(self, job_id: int, note: str) -> None:
        """Record why, without changing what.

        Used when a job is heading for delivery but the thing being delivered is
        an apology — the state says "there is a message to send", the note says
        "because this went wrong".
        """
        self._conn.execute("UPDATE jobs SET note = ? WHERE id = ?", (note, job_id))
        self._conn.commit()

    def deliverable(self, limit: int = 5) -> list[Job]:

        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY id LIMIT ?", (READY, limit)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def settle(self, job_id: int, state: str, note: str = "") -> None:
        """Mark a job terminal. Rows are never deleted here — only by ``purge``."""
        self._conn.execute(
            "UPDATE jobs SET state = ?, note = ?, settled_at = ?, "
            "lease_owner = NULL, lease_expires = NULL WHERE id = ?",
            (state, note, time.time(), job_id),
        )
        self._conn.commit()

    def stale(self, timeout_seconds: float) -> list[Job]:
        """Jobs that have been running too long. Settling them is the caller's call.

        A wedged worker holds no lock anyone waits on, so the cost of ignoring
        this is quieter than a deadlock and worse: a chat that was told to expect
        an answer never hears back, and the slot stays occupied forever. The
        caller decides between settling silently and saying something, which is
        why this only reports.

        Measured from ``started_at``, not ``created_at``: a job that queued
        behind a busy pool for ten minutes has not used any of its own time yet,
        and timing it out on arrival would retire it before it ran.
        """
        cutoff = time.time() - timeout_seconds
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state = ? AND COALESCE(started_at, created_at) < ?",
            (RUNNING, cutoff),
        ).fetchall()
        return [_row_to_job(r) for r in rows]



    def release(self, owner: str) -> int:
        """Hand back this owner's running jobs on shutdown, rather than stranding them."""
        cursor = self._conn.execute(
            "UPDATE jobs SET state = ?, lease_owner = NULL, lease_expires = NULL "
            "WHERE state = ? AND lease_owner = ?",
            (QUEUED, RUNNING, owner),
        )
        self._conn.commit()
        return cursor.rowcount

    # ---- quotas -----------------------------------------------------------

    def active_for_chat(self, chat_guid: str) -> int:
        """Jobs this chat has in flight. One at a time is the point."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE chat_guid = ? AND state IN (?, ?, ?)",
            (chat_guid, QUEUED, RUNNING, READY),
        ).fetchone()
        return int(row[0])

    def in_flight(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN (?, ?)", (QUEUED, RUNNING)
        ).fetchone()
        return int(row[0])

    def seconds_since_last(self, chat_guid: str) -> float | None:
        """Age of this chat's most recent job, or None if it has never had one."""
        row = self._conn.execute(
            "SELECT MAX(created_at) FROM jobs WHERE chat_guid = ?", (chat_guid,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        return time.time() - float(row[0])

    def count_since(self, chat_guid: str, seconds: float) -> int:
        cutoff = time.time() - seconds
        row = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE chat_guid = ? AND created_at > ?",
            (chat_guid, cutoff),
        ).fetchone()
        return int(row[0])

    # ---- observability ----------------------------------------------------

    def stats(self) -> JobStats:
        rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
        counts = {r["state"]: int(r["n"]) for r in rows}
        return JobStats(
            queued=counts.get(QUEUED, 0),
            running=counts.get(RUNNING, 0),
            ready=counts.get(READY, 0),
            delivered=counts.get(DELIVERED, 0),
            failed=counts.get(FAILED, 0),
            expired=counts.get(EXPIRED, 0),
            blocked=counts.get(BLOCKED, 0),
        )

    def recent(self, chat_guid: str | None = None, limit: int = 20) -> list[Job]:
        if chat_guid is None:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE chat_guid = ? ORDER BY id DESC LIMIT ?",
                (chat_guid, limit),
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def chats(self, limit: int = 50) -> list[str]:
        rows = self._conn.execute(
            "SELECT chat_guid, MAX(id) AS m FROM jobs GROUP BY chat_guid ORDER BY m DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["chat_guid"] for r in rows]

    def purge(self, older_than_seconds: float = 7 * 24 * 3600) -> int:
        """Delete settled rows past their useful life. In-flight rows are never touched."""
        cutoff = time.time() - older_than_seconds
        marks = ",".join("?" for _ in TERMINAL)
        cursor = self._conn.execute(
            f"DELETE FROM jobs WHERE state IN ({marks}) AND settled_at IS NOT NULL "
            f"AND settled_at < ?",
            (*TERMINAL, cutoff),
        )
        self._conn.commit()
        return cursor.rowcount
