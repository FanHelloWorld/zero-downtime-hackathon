"""BackgroundLoop is what keeps a blocking loop from wedging the event loop,
so its startup handshake and shutdown path get tested directly."""

from __future__ import annotations

import threading
import time

import pytest

from plug.service import AlreadyRunning, BackgroundLoop


class FakeLoop:
    def __init__(self, *, crash: bool = False) -> None:
        self.started = threading.Event()
        self.stopped = False
        self.handle_signals: bool | None = None
        self._crash = crash

    def run(self, *, handle_signals: bool = True):
        self.handle_signals = handle_signals
        self.started.set()
        if self._crash:
            raise RuntimeError("loop exploded")
        while not self.stopped:
            time.sleep(0.01)

    def request_stop(self, *_: object) -> None:
        self.stopped = True


class Closer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_start_runs_the_loop_on_a_worker_thread():
    fake = FakeLoop()
    bg = BackgroundLoop("test", lambda: (fake, []), single_instance=False)
    bg.start()
    try:
        assert fake.started.wait(2)
        assert bg.running
        # uvicorn owns signals, and signal.signal would raise off-main-thread.
        assert fake.handle_signals is False
    finally:
        bg.stop()

    assert fake.stopped


def test_build_failure_surfaces_from_start():
    """A missing API key or Full Disk Access must fail uvicorn startup loudly,
    not leave a server that answers /health while doing nothing."""

    def build():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    bg = BackgroundLoop("test", build, single_instance=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        bg.start()
    assert not bg.running


def test_a_loop_that_dies_later_is_reported_not_raised():
    """start() must not raise for a run-time crash — whether it had returned
    yet is a thread race, and non-deterministic startup behaviour is worse than
    a health check that goes red."""
    bg = BackgroundLoop("test", lambda: (FakeLoop(crash=True), []), single_instance=False)
    bg.start()  # must not raise

    for _ in range(200):
        if not bg.running:
            break
        time.sleep(0.01)

    health = bg.health()
    assert health["running"] is False
    assert "loop exploded" in health["error"]
    assert bg.build_error is None, "a run failure must not be recorded as a build failure"


def test_build_and_run_failures_are_distinguished():
    def bad_build():
        raise RuntimeError("no credentials")

    bg = BackgroundLoop("test", bad_build, single_instance=False)
    with pytest.raises(RuntimeError):
        bg.start()
    assert bg.build_error is not None
    assert bg.run_error is None


def test_resources_are_closed_when_the_thread_exits():
    """SQLite handles opened in the worker thread must not leak on shutdown."""
    fake, closer = FakeLoop(), Closer()
    bg = BackgroundLoop("test", lambda: (fake, [closer]), single_instance=False)
    bg.start()
    bg.stop()

    for _ in range(200):
        if closer.closed:
            break
        time.sleep(0.01)
    assert closer.closed


def test_start_is_idempotent():
    """Mounting an app whose lifespan also starts the loop must not double-start."""
    builds = []

    def build():
        builds.append(1)
        return FakeLoop(), []

    bg = BackgroundLoop("test", build, single_instance=False)
    bg.start()
    bg.start()
    try:
        assert len(builds) == 1
    finally:
        bg.stop()


def test_stop_is_safe_before_start():
    BackgroundLoop("test", lambda: (FakeLoop(), []), single_instance=False).stop()


def test_slow_build_times_out():
    def build():
        time.sleep(5)
        return FakeLoop(), []

    bg = BackgroundLoop("test", build, single_instance=False)
    with pytest.raises(TimeoutError):
        bg.start(timeout=0.2)


# ---- single-instance lock -------------------------------------------------


def test_second_instance_is_refused(monkeypatch, tmp_path):
    """Two watchdogs against one cursor file interleave and make stats lie."""
    monkeypatch.setattr("plug.service.STATE_DIR", tmp_path)
    monkeypatch.delenv("PLUG_ALLOW_MULTIPLE", raising=False)

    first = BackgroundLoop("locked", lambda: (FakeLoop(), []))
    first.start()
    try:
        second = BackgroundLoop("locked", lambda: (FakeLoop(), []))
        with pytest.raises(AlreadyRunning):
            second.start()
    finally:
        first.stop()


def test_lock_is_released_on_stop(monkeypatch, tmp_path):
    monkeypatch.setattr("plug.service.STATE_DIR", tmp_path)
    monkeypatch.delenv("PLUG_ALLOW_MULTIPLE", raising=False)

    first = BackgroundLoop("cycle", lambda: (FakeLoop(), []))
    first.start()
    first.stop()

    second = BackgroundLoop("cycle", lambda: (FakeLoop(), []))
    second.start()  # must not raise
    second.stop()


def test_failed_build_does_not_strand_the_lock(monkeypatch, tmp_path):
    """A startup failure must be recoverable without deleting a lock file."""
    monkeypatch.setattr("plug.service.STATE_DIR", tmp_path)
    monkeypatch.delenv("PLUG_ALLOW_MULTIPLE", raising=False)

    def bad_build():
        raise RuntimeError("no credentials")

    broken = BackgroundLoop("retry", bad_build)
    with pytest.raises(RuntimeError, match="no credentials"):
        broken.start()

    fixed = BackgroundLoop("retry", lambda: (FakeLoop(), []))
    fixed.start()
    fixed.stop()


def test_escape_hatch_allows_multiple(monkeypatch, tmp_path):
    monkeypatch.setattr("plug.service.STATE_DIR", tmp_path)
    monkeypatch.setenv("PLUG_ALLOW_MULTIPLE", "1")

    first = BackgroundLoop("multi", lambda: (FakeLoop(), []))
    second = BackgroundLoop("multi", lambda: (FakeLoop(), []))
    first.start()
    second.start()
    first.stop()
    second.stop()
