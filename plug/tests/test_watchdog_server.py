"""Watchdog server: reads chat.db, pools to disk, and does nothing else."""

from __future__ import annotations

from dataclasses import asdict

from watchdog.server import WatchdogServer, WatchdogStats
from watchdog.state import WatchdogState

from .conftest import imported_roots, make_message


class StubDB:
    """Stands in for chat.db so the loop can be tested without Full Disk Access."""

    def __init__(self, ticks: list[list]):
        self._ticks = ticks

    def messages_after(self, cursor: int, limit: int = 200):
        return self._ticks.pop(0) if self._ticks else []

    def max_rowid(self) -> int:
        return 0


def build(config, tmp_path, spool, ticks) -> WatchdogServer:
    state = WatchdogState(tmp_path / "watchdog.db")
    state.set_cursor(0)
    return WatchdogServer(config, StubDB(ticks), state, spool, echo=False)


def test_stats_are_serializable():
    """Stats is a slotted dataclass; the stop event must not reach for __dict__."""
    assert asdict(WatchdogStats(ticks=1, spooled=2))["spooled"] == 2


def test_tick_pools_messages_to_disk(config, tmp_path, spool):
    server = build(config, tmp_path, spool, [[make_message(rowid=10), make_message(rowid=11)]])

    assert server.tick() == 2
    assert spool.stats().pending == 2


def test_cursor_advances_past_everything_examined(config, tmp_path, spool):
    server = build(config, tmp_path, spool, [[make_message(rowid=10), make_message(rowid=11)]])
    server.tick()
    assert server.state.get_cursor() == 11


def test_filtered_messages_still_advance_the_cursor(config, tmp_path, spool):
    """Otherwise a disabled-service message is re-read on every tick forever."""
    config.chats.services = ["iMessage"]
    server = build(config, tmp_path, spool, [[make_message(rowid=42, service="SMS")]])

    assert server.tick() == 0
    assert server.state.get_cursor() == 42
    assert server.stats.filtered == 1
    assert spool.stats().pending == 0


def test_duplicate_rows_are_not_pooled_twice(config, tmp_path, spool):
    """A restart that re-reads a window must not produce a second reply."""
    msg = make_message(rowid=5)
    server = build(config, tmp_path, spool, [[msg], [msg]])

    assert server.tick() == 1
    server.state.set_cursor(0)  # simulate a cursor rollback on restart
    assert server.tick() == 0
    assert server.stats.duplicates == 1
    assert spool.stats().pending == 1


def test_empty_tick_is_cheap_and_leaves_cursor_alone(config, tmp_path, spool):
    server = build(config, tmp_path, spool, [])
    server.state.set_cursor(99)
    assert server.tick() == 0
    assert server.state.get_cursor() == 99


def test_tick_errors_do_not_kill_the_loop(config, tmp_path, spool):
    class Exploding:
        def messages_after(self, cursor, limit=200):
            raise RuntimeError("chat.db went away")

        def max_rowid(self):
            return 0

    state = WatchdogState(tmp_path / "watchdog.db")
    server = WatchdogServer(config, Exploding(), state, spool, echo=False)
    server.request_stop()
    server.run()  # must return rather than propagate


def test_watchdog_never_touches_the_reply_path():
    """The server must not import anything agent-side.

    This is what lets the watchdog run without an API key or Automation
    permission, and be restarted freely while the supervisor is down.
    """
    import watchdog.server as mod

    roots = imported_roots(mod)
    assert "anthropic" not in roots
    assert "supervisor_agent" not in roots
