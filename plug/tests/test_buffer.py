from __future__ import annotations

from supervisor_agent.buffer import Buffer

from .conftest import make_item


def now_of(buf: Buffer, offset: float) -> float:
    """Advance the clock relative to when the last item was buffered."""
    return max(buf._last_seen.values()) + offset


def test_holds_items_until_quiet(config):
    config.supervisor.debounce_seconds = 5
    buf = Buffer(config)
    buf.add(make_item(1, body="hey"))
    assert buf.due(now_of(buf, 0)) == []
    assert buf.pending_chats() == 1


def test_releases_after_debounce(config):
    config.supervisor.debounce_seconds = 4
    buf = Buffer(config)
    buf.add(make_item(1, body="hey"))
    buf.add(make_item(2, body="you around?"))

    assert buf.due(now_of(buf, 3.9)) == []

    released = buf.due(now_of(buf, 4.1))
    assert len(released) == 1
    assert [m.body for m in released[0].batch.messages] == ["hey", "you around?"]
    assert buf.pending_chats() == 0


def test_released_batch_carries_pool_ids_for_acknowledgement(config):
    config.supervisor.debounce_seconds = 0
    buf = Buffer(config)
    buf.add(make_item(11))
    buf.add(make_item(12))

    work = buf.due(now_of(buf, 1))[0]
    assert work.ids == [11, 12], "without these the items could never be acked"


def test_size_cap_releases_early(config):
    config.supervisor.debounce_seconds = 999
    config.supervisor.max_batch = 3
    buf = Buffer(config)
    for i in range(3):
        buf.add(make_item(i, body=f"m{i}"))

    released = buf.due(now_of(buf, 0))
    assert len(released) == 1
    assert len(released[0].ids) == 3


def test_chats_are_debounced_independently(config):
    config.supervisor.debounce_seconds = 4
    buf = Buffer(config)
    buf.add(make_item(1, chat_guid="chat-a"))
    buf.add(make_item(2, chat_guid="chat-b"))
    assert buf.pending_chats() == 2

    released = buf.due(now_of(buf, 5))
    assert {w.chat_guid for w in released} == {"chat-a", "chat-b"}


def test_drain_releases_everything(config):
    config.supervisor.debounce_seconds = 999
    buf = Buffer(config)
    buf.add(make_item(1))
    buf.add(make_item(2, chat_guid="chat-b"))

    assert len(buf.drain()) == 2
    assert buf.pending_items() == 0


def test_max_attempts_is_carried_through(config):
    config.supervisor.debounce_seconds = 0
    buf = Buffer(config)
    buf.add(make_item(1, attempts=1))
    buf.add(make_item(2, attempts=3))

    work = buf.due(now_of(buf, 1))[0]
    assert work.max_attempts == 3


def test_group_transcript_attributes_speakers(config, group_message):
    from plug.spool import SpoolItem

    buf = Buffer(config)
    buf.add(SpoolItem(id=1, attempts=1, message=group_message))
    work = buf.drain()[0]
    assert work.batch.transcript().startswith(f"{group_message.handle}: ")


def test_direct_transcript_is_bare(config):
    buf = Buffer(config)
    buf.add(make_item(1, body="hey"))
    work = buf.drain()[0]
    assert work.batch.transcript() == "hey"
