"""Live checks against chat.db. Skipped when Full Disk Access isn't granted."""

from __future__ import annotations

import sqlite3

import pytest

from plug.config import CHAT_DB

try:
    from watchdog.db import ChatDB

    _available = ChatDB().max_rowid() > 0
except Exception:  # pragma: no cover
    _available = False

pytestmark = pytest.mark.skipif(not _available, reason="chat.db not readable here")


def test_connection_is_read_only():
    with ChatDB() as db:
        with pytest.raises(sqlite3.OperationalError):
            db._conn.execute("CREATE TABLE plug_should_not_exist (x INT)")


def test_wal_mode_connection_sees_newer_rows_than_immutable():
    """`immutable=1` ignores the -wal file and silently hides the newest messages.

    That is precisely the data the watchdog exists to read, so the production
    connection must never use it.
    """
    with ChatDB() as db:
        live = db.max_rowid()
        assert db.journal_mode().lower() == "wal"

    immutable = sqlite3.connect(f"file:{CHAT_DB}?immutable=1", uri=True)
    stale = immutable.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
    immutable.close()

    assert live >= stale


def test_cursor_at_high_water_mark_returns_nothing():
    with ChatDB() as db:
        assert db.messages_after(db.max_rowid()) == []


def test_messages_are_inbound_human_and_decoded():
    with ChatDB() as db:
        recent = db.messages_after(max(0, db.max_rowid() - 800), limit=100)

    for m in recent:
        assert m.body.strip(), "empty bodies must be filtered out"
        assert m.rowid > 0
        assert m.chat_guid
        assert m.style in (43, 45)


def test_results_are_ordered_and_unique():
    with ChatDB() as db:
        recent = db.messages_after(max(0, db.max_rowid() - 2000), limit=200)

    rowids = [m.rowid for m in recent]
    assert rowids == sorted(rowids)
    assert len(rowids) == len(set(rowids)), "a message in several chats must appear once"
