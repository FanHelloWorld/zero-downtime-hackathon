"""The disk pool is the entire contract between the two servers, so its
durability and lease semantics get tested hard."""

from __future__ import annotations

import time

from plug.spool import DEAD, DONE, DROPPED, PENDING, Spool

from .conftest import make_message


def test_enqueue_returns_count_and_is_idempotent(spool):
    messages = [make_message(rowid=1), make_message(rowid=2)]
    assert spool.enqueue(messages) == 2
    # A watchdog restart that re-reads the same window must not double-queue.
    assert spool.enqueue(messages) == 0
    assert spool.stats().pending == 2


def test_enqueue_empty_is_a_noop(spool):
    assert spool.enqueue([]) == 0


def test_lease_claims_items_and_hides_them_from_other_consumers(spool):
    spool.enqueue([make_message(rowid=i) for i in range(3)])

    first = spool.lease("worker-a", limit=2)
    assert len(first) == 2

    second = spool.lease("worker-b", limit=10)
    assert [i.message.rowid for i in second] == [2], "leased items must not be handed out twice"


def test_lease_preserves_message_fields(spool):
    spool.enqueue([make_message(rowid=7, body="hello there", handle="+15550001111")])
    item = spool.lease()[0]
    assert item.message.rowid == 7
    assert item.message.body == "hello there"
    assert item.message.handle == "+15550001111"
    assert item.message.service == "iMessage"


def test_expired_lease_is_reclaimed(spool):
    """A supervisor that crashes mid-reply must not lose the message."""
    spool.enqueue([make_message(rowid=1)])
    spool.lease("crashed-worker", lease_seconds=0.01)
    assert spool.lease("fresh-worker") == []  # not yet expired within the same instant

    time.sleep(0.05)
    reclaimed = spool.lease("fresh-worker")
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2, "reclaim counts as another attempt"


def test_ack_is_terminal(spool):
    spool.enqueue([make_message(rowid=1)])
    item = spool.lease()[0]
    spool.ack([item.id])

    assert spool.lease() == []
    assert spool.stats().done == 1
    assert spool.stats().depth == 0


def test_drop_is_terminal_and_records_a_reason(spool):
    spool.enqueue([make_message(rowid=1)])
    item = spool.lease()[0]
    spool.drop([item.id], "matched deny pattern")

    assert spool.lease() == []
    assert spool.stats().dropped == 1


def test_nack_returns_work_for_retry(spool):
    spool.enqueue([make_message(rowid=1)])
    item = spool.lease()[0]
    spool.nack([item.id], "api error", max_attempts=5)

    again = spool.lease()
    assert len(again) == 1
    assert again[0].attempts == 2


def test_nack_dead_letters_after_max_attempts(spool):
    """A message that reliably crashes the agent must stop being retried."""
    spool.enqueue([make_message(rowid=1)])
    for _ in range(3):
        item = spool.lease()[0]
        spool.nack([item.id], "boom", max_attempts=3)

    assert spool.lease() == [], "dead-lettered work must not be re-leased"
    assert spool.stats().dead == 1
    assert len(spool.dead_letters()) == 1


def test_dead_letters_can_be_requeued(spool):
    spool.enqueue([make_message(rowid=1)])
    for _ in range(3):
        item = spool.lease()[0]
        spool.nack([item.id], "boom", max_attempts=3)

    assert spool.requeue_dead() == 1
    assert len(spool.lease()) == 1


def test_stats_depth_counts_pending_and_in_flight(spool):
    spool.enqueue([make_message(rowid=i) for i in range(4)])
    spool.lease(limit=2)

    stats = spool.stats()
    assert stats.pending == 2
    assert stats.leased == 2
    assert stats.depth == 4


def test_oldest_pending_age_surfaces_a_backlog(spool):
    spool.enqueue([make_message(rowid=1)])
    time.sleep(0.05)
    assert spool.stats().oldest_pending_age > 0


def test_purge_only_removes_settled_rows(spool):
    spool.enqueue([make_message(rowid=1), make_message(rowid=2)])
    item = spool.lease(limit=1)[0]
    spool.ack([item.id])

    # Nothing is old enough yet.
    assert spool.purge(older_than_seconds=3600) == 0
    # With a zero window the settled row goes and the pending one stays.
    assert spool.purge(older_than_seconds=0) == 1
    assert spool.stats().pending == 1


def test_survives_reopen(tmp_path):
    """Independent restarts are the whole point — state must be on disk."""
    path = tmp_path / "spool.db"
    first = Spool(path)
    first.enqueue([make_message(rowid=99, body="persisted")])
    first.close()

    second = Spool(path)
    items = second.lease()
    assert len(items) == 1
    assert items[0].message.body == "persisted"
    second.close()


def test_two_connections_share_the_pool(tmp_path):
    """The watchdog and supervisor are separate processes on one file."""
    path = tmp_path / "spool.db"
    producer = Spool(path)
    consumer = Spool(path)

    producer.enqueue([make_message(rowid=1, body="from the watchdog")])
    items = consumer.lease("supervisor")

    assert len(items) == 1
    assert items[0].message.body == "from the watchdog"

    consumer.ack([items[0].id])
    assert producer.stats().done == 1

    producer.close()
    consumer.close()
