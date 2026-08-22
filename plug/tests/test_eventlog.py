"""The indexed event log — the half of the log a UI can query.

The JSONL file remains the thing you read when something is wrong. This is the
thing a browser streams from, so what matters here is that the cursor is
trustworthy and that a broken store never takes the pipeline down with it.
"""

from __future__ import annotations

import time

import pytest

from plug import events as events_mod
from plug.eventlog import EventStore, record


@pytest.fixture()
def store_path(tmp_path):
    return tmp_path / "events.db"


def write(path, stage="watchdog", event="spooled", chat="chat-a", **detail):
    return record(stage, event, chat=chat, detail=detail, path=path)


# ---- the cursor -----------------------------------------------------------


def test_ids_increase_and_are_returned(store_path):
    first = write(store_path, count=1)
    second = write(store_path, count=2)
    assert first and second and second > first


def test_since_returns_only_what_came_after_in_the_order_it_happened(store_path):
    ids = [write(store_path, n=i) for i in range(4)]
    after = EventStore(store_path).since(ids[1])
    assert [e["detail"]["n"] for e in after] == [2, 3], "oldest first, cursor exclusive"


def test_since_is_bounded(store_path):
    for i in range(10):
        write(store_path, n=i)
    assert len(EventStore(store_path).since(0, limit=3)) == 3


def test_max_id_on_an_empty_store_is_zero(store_path):
    assert EventStore(store_path).max_id() == 0


def test_a_reader_can_resume_from_where_it_stopped(store_path):
    write(store_path, n=0)
    store = EventStore(store_path)
    seen = store.since(0)
    cursor = seen[-1]["id"]
    write(store_path, n=1)
    assert [e["detail"]["n"] for e in store.since(cursor)] == [1]


# ---- reading --------------------------------------------------------------


def test_recent_is_newest_first(store_path):
    for i in range(3):
        write(store_path, n=i)
    assert [e["detail"]["n"] for e in EventStore(store_path).recent(3)] == [2, 1, 0]


def test_recent_filters_by_stage_and_chat(store_path):
    write(store_path, stage="worker", event="started", chat="chat-a")
    write(store_path, stage="agent", event="start", chat="chat-b")
    store = EventStore(store_path)
    assert len(store.recent(10, stage="worker")) == 1
    assert len(store.recent(10, chat="chat-b")) == 1


def test_counts_are_keyed_by_stage_and_event(store_path):
    write(store_path, stage="agent", event="planned")
    write(store_path, stage="agent", event="planned")
    write(store_path, stage="send", event="delivered")
    counts = EventStore(store_path).counts()
    assert counts["agent/planned"] == 2
    assert counts["send/delivered"] == 1


def test_counts_respect_the_window(store_path):
    record("agent", "planned", chat=None, detail={}, ts=time.time() - 10_000, path=store_path)
    write(store_path, stage="agent", event="planned")
    assert EventStore(store_path).counts(since_seconds=3600) == {"agent/planned": 1}


def test_the_histogram_has_one_bucket_per_slot(store_path):
    write(store_path)
    hist = EventStore(store_path).histogram(since_seconds=3600, buckets=12)
    assert len(hist) == 12
    assert sum(hist) == 1


def test_chats_are_listed_most_recent_first(store_path):
    write(store_path, chat="old")
    write(store_path, chat="new")
    assert EventStore(store_path).chats() == ["new", "old"]
    write(store_path, chat="-")
    assert "-" not in EventStore(store_path).chats(), "the no-chat placeholder is not a chat"


# ---- detail ---------------------------------------------------------------


def test_detail_round_trips(store_path):
    write(store_path, count=3, tools=["a", "b"], nested={"k": 1})
    detail = EventStore(store_path).recent(1)[0]["detail"]
    assert detail == {"count": 3, "tools": ["a", "b"], "nested": {"k": 1}}


def test_unserialisable_detail_is_stringified_rather_than_lost(store_path):
    write(store_path, when=time.gmtime(0))
    assert "detail" in EventStore(store_path).recent(1)[0]


def test_a_corrupt_detail_column_degrades_to_empty(store_path):
    write(store_path, n=1)
    store = EventStore(store_path)
    store._conn.execute("UPDATE events SET detail = 'not json'")
    store._conn.commit()
    assert store.recent(1)[0]["detail"] == {}


# ---- failure --------------------------------------------------------------


def test_an_unwritable_store_returns_none_instead_of_raising():
    """emit() is called from error handlers. It must not raise a new error."""
    assert record("x", "y", chat=None, detail={}, path="/nope/nowhere/events.db") is None


def test_the_path_is_resolved_at_call_time(tmp_path, monkeypatch):
    """A default bound at import would pin the real store and defeat isolation."""
    from plug import eventlog as eventlog_mod

    redirected = tmp_path / "redirected.db"
    monkeypatch.setattr(eventlog_mod, "EVENTS_DB", redirected)
    assert record("x", "y", chat=None, detail={}) is not None
    assert redirected.exists()


# ---- housekeeping ---------------------------------------------------------


def test_purge_removes_only_what_is_old(store_path):
    record("old", "e", chat=None, detail={}, ts=time.time() - 10_000, path=store_path)
    write(store_path)
    assert EventStore(store_path).purge(older_than_seconds=3600) == 1
    assert EventStore(store_path).max_id() >= 1
    assert len(EventStore(store_path).recent(10)) == 1


# ---- the pairing with the JSONL log ---------------------------------------


def test_emit_writes_to_both_logs(tmp_path, monkeypatch):
    from plug import eventlog as eventlog_mod

    monkeypatch.setattr(events_mod, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(eventlog_mod, "EVENTS_DB", tmp_path / "events.db")

    events_mod.emit("worker", "ready", chat="secret-guid", job="j_1")

    assert (tmp_path / "events.jsonl").read_text().count("\n") == 1
    stored = EventStore(tmp_path / "events.db").recent(1)[0]
    assert stored["stage"] == "worker" and stored["event"] == "ready"
    assert stored["detail"] == {"job": "j_1"}


def test_the_stored_chat_key_is_anonymised(tmp_path, monkeypatch):
    """The UI reads this table. It must not be the place raw guids leak."""
    from plug import eventlog as eventlog_mod

    monkeypatch.setattr(events_mod, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(eventlog_mod, "EVENTS_DB", tmp_path / "events.db")

    events_mod.emit("agent", "start", chat="iMessage;+;real-group-guid")
    stored = EventStore(tmp_path / "events.db").recent(1)[0]
    assert stored["chat"] == events_mod.anon("iMessage;+;real-group-guid")
    assert "real-group-guid" not in str(stored)
