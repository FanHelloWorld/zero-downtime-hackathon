from __future__ import annotations

from supervisor_agent.memory import Memory


def test_history_round_trips_oldest_first(memory):
    memory.append_history("chat-a", "user", "one")
    memory.append_history("chat-a", "assistant", "two")
    memory.append_history("chat-b", "user", "other chat")

    history = memory.recent_history("chat-a", 10)
    assert history == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]


def test_history_is_capped_and_keeps_the_newest(memory):
    for i in range(20):
        memory.append_history("chat-a", "user", f"m{i}")
    history = memory.recent_history("chat-a", 5)
    assert [h["content"] for h in history] == ["m15", "m16", "m17", "m18", "m19"]


def test_send_counters_ignore_dry_runs(memory):
    memory.record_send("chat-a", "hi", dry_run=True)
    assert memory.sends_in_last_hour("chat-a") == 0
    memory.record_send("chat-a", "hi", dry_run=False)
    assert memory.sends_in_last_hour("chat-a") == 1
    assert memory.sends_in_last_hour() == 1


def test_last_send_ts_tracks_only_real_sends(memory):
    assert memory.last_send_ts("chat-a") is None
    memory.record_send("chat-a", "hi", dry_run=True)
    assert memory.last_send_ts("chat-a") is None
    memory.record_send("chat-a", "hi", dry_run=False)
    assert memory.last_send_ts("chat-a") is not None
