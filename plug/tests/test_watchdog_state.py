from __future__ import annotations

from watchdog.state import WatchdogState


def test_cold_start_seeds_at_current_high_water_mark(tmp_path):
    """Without this, a first run would spool all 156k historical messages."""
    state = WatchdogState(tmp_path / "watchdog.db")
    assert state.get_cursor() is None
    assert state.seed_cursor(176_633) == 176_633
    assert state.get_cursor() == 176_633
    state.close()


def test_seed_is_idempotent_across_restarts(tmp_path):
    path = tmp_path / "watchdog.db"
    state = WatchdogState(path)
    state.seed_cursor(1000)
    state.set_cursor(1050)
    state.close()

    again = WatchdogState(path)
    # A restart must resume from where we left off, not re-seed to the newest row.
    assert again.seed_cursor(200_000) == 1050
    again.close()


def test_cursor_is_private_to_the_watchdog(tmp_path):
    """It lives in the watchdog's own file, not in supervisor state."""
    from supervisor_agent.memory import Memory

    assert not hasattr(Memory(tmp_path / "supervisor.db"), "get_cursor")
