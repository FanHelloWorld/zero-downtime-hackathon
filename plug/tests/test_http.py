"""HTTP surface of both ASGI apps.

The real loops are replaced with stubs so these tests never open chat.db, never
call the model, and never touch the pool in ``~/.plug``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from plug.safety import pause, resume
from plug.spool import Spool
from watchdog.server import WatchdogStats
from supervisor_agent.server import SupervisorStats

from .conftest import make_message


class StubBackgroundLoop:
    """Mimics BackgroundLoop without spawning a thread."""

    def __init__(self, inner, *, running: bool = True, error: str | None = None):
        self.loop = inner
        self._running = running
        self._error = error
        self.started = False
        self.stopped = False

    def start(self, **_):
        self.started = True

    def stop(self, **_):
        self.stopped = True

    @property
    def running(self):
        return self._running

    def health(self):
        return {"name": "stub", "running": self._running, "finished": False, "error": self._error}


@dataclass
class StubState:
    cursor: int = 4242

    def get_cursor(self):
        return self.cursor


class StubWatchdog:
    def __init__(self):
        self.stats = WatchdogStats(ticks=5, seen=3, spooled=2)
        self.state = StubState()


class StubSupervisor:
    owner = "test-owner"
    workers = None

    def __init__(self):
        self.stats = SupervisorStats(ticks=9, sent=2, blocked=1)



@pytest.fixture(autouse=True)
def _unpaused():
    resume()
    yield
    resume()


@pytest.fixture()
def isolated_spool(tmp_path, monkeypatch):
    """Point both apps at a throwaway pool instead of the user's real one."""
    path = tmp_path / "spool.db"
    factory = lambda: Spool(path)  # noqa: E731
    import supervisor_agent.main as sup_main
    import watchdog.main as wd_main

    monkeypatch.setattr(wd_main, "Spool", factory)
    monkeypatch.setattr(sup_main, "Spool", factory)
    return factory


@pytest.fixture()
def isolated_watchdog_state(tmp_path, monkeypatch):
    """Keep the real ~/.plug/watchdog.db out of the tests."""
    import watchdog.main as wd_main
    from watchdog.state import WatchdogState

    monkeypatch.setattr(wd_main, "WatchdogState", lambda: WatchdogState(tmp_path / "watchdog.db"))


@pytest.fixture()
def watchdog_client(monkeypatch, isolated_spool, isolated_watchdog_state):
    import watchdog.main as wd_main

    monkeypatch.setattr(wd_main, "loop", StubBackgroundLoop(StubWatchdog()))
    with TestClient(wd_main.app) as client:
        yield client


@pytest.fixture()
def isolated_jobs(tmp_path, monkeypatch):
    """Keep the real ~/.plug/jobs.db out of the tests."""
    import supervisor_agent.main as sup_main
    from supervisor_agent.jobs import JobStore

    factory = lambda: JobStore(tmp_path / "jobs.db")  # noqa: E731
    monkeypatch.setattr(sup_main, "JobStore", factory)
    return factory


@pytest.fixture()
def supervisor_client(monkeypatch, isolated_spool, isolated_jobs, tmp_path):
    import supervisor_agent.main as sup_main
    from supervisor_agent.memory import Memory

    monkeypatch.setattr(sup_main, "loop", StubBackgroundLoop(StubSupervisor()))
    monkeypatch.setattr(sup_main, "Memory", lambda: Memory(tmp_path / "supervisor.db"))
    with TestClient(sup_main.app) as client:
        yield client



# ---- watchdog -------------------------------------------------------------


def test_watchdog_health_ok(watchdog_client):
    body = watchdog_client.get("/health").json()
    assert body["running"] is True


def test_watchdog_health_is_503_when_the_loop_died(monkeypatch, isolated_spool):
    """A dead loop must fail the health check so a supervisor restarts us."""
    import watchdog.main as wd_main

    monkeypatch.setattr(
        wd_main, "loop", StubBackgroundLoop(StubWatchdog(), running=False, error="boom")
    )
    with TestClient(wd_main.app) as client:
        assert client.get("/health").status_code == 503


def test_watchdog_status_reports_cursor_and_pool(watchdog_client, isolated_spool, tmp_path):
    with isolated_spool() as spool:
        spool.enqueue([make_message(rowid=1)])
    from watchdog.state import WatchdogState

    with WatchdogState(tmp_path / "watchdog.db") as state:
        state.set_cursor(4242)

    body = watchdog_client.get("/status").json()
    assert body["cursor"] == 4242
    assert body["stats"]["spooled"] == 2
    assert body["pool"]["pending"] == 1
    assert body["config"]["poll_interval_seconds"] > 0


def test_watchdog_metrics_are_prometheus_text(watchdog_client):
    resp = watchdog_client.get("/metrics")
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "plug_watchdog_up 1" in body
    assert "plug_watchdog_spooled_total 2" in body
    assert "plug_pool_pending" in body


def test_lifespan_starts_and_stops_the_loop(monkeypatch, isolated_spool):
    import watchdog.main as wd_main

    stub = StubBackgroundLoop(StubWatchdog())
    monkeypatch.setattr(wd_main, "loop", stub)

    with TestClient(wd_main.app):
        assert stub.started
    assert stub.stopped, "the loop must be stopped on shutdown"


# ---- supervisor -----------------------------------------------------------


def test_supervisor_health_reports_mode(supervisor_client):
    body = supervisor_client.get("/health").json()
    assert body["running"] is True
    assert body["paused"] is False
    assert "dry_run" in body


def test_supervisor_status(supervisor_client, isolated_spool):
    with isolated_spool() as spool:
        spool.enqueue([make_message(rowid=1), make_message(rowid=2)])

    body = supervisor_client.get("/status").json()
    assert body["owner"] == "test-owner"
    assert body["stats"]["sent"] == 2
    assert body["pool"]["pending"] == 2
    assert body["sends_last_hour"] == 0
    assert body["config"]["model"]


def test_kill_switch_over_http(supervisor_client):
    assert supervisor_client.post("/pause").json() == {"paused": True}
    assert supervisor_client.get("/health").json()["paused"] is True

    assert supervisor_client.post("/resume").json() == {"paused": False}
    assert supervisor_client.get("/health").json()["paused"] is False


def test_paused_state_is_visible_in_metrics(supervisor_client):
    pause()
    assert "plug_supervisor_paused 1" in supervisor_client.get("/metrics").text


def test_dead_letter_view_omits_message_bodies(supervisor_client, isolated_spool):
    """This is an ops endpoint; it must not become a way to read your texts."""
    with isolated_spool() as spool:
        spool.enqueue([make_message(rowid=1, body="something private")])
        for _ in range(3):
            item = spool.lease()[0]
            spool.nack([item.id], "boom", max_attempts=3)

    body = supervisor_client.get("/dead").json()
    assert body["count"] == 1
    assert "something private" not in str(body)
    assert body["items"][0]["chars"] == len("something private")


def test_dead_letters_can_be_requeued_over_http(supervisor_client, isolated_spool):
    with isolated_spool() as spool:
        spool.enqueue([make_message(rowid=1)])
        for _ in range(3):
            item = spool.lease()[0]
            spool.nack([item.id], "boom", max_attempts=3)

    assert supervisor_client.post("/dead/requeue").json() == {"requeued": 1}
    with isolated_spool() as spool:
        assert spool.stats().pending == 1


def test_status_does_not_touch_the_loop_threads_connections(tmp_path, monkeypatch):
    """Regression: /status once read the loop's own WatchdogState.

    SQLite refuses cross-thread use, so that raised ProgrammingError as soon as
    the endpoint ran on a threadpool worker rather than the loop thread. This
    test drives a genuinely threaded BackgroundLoop so the boundary is real.
    """
    import watchdog.main as wd_main
    from plug.service import BackgroundLoop
    from plug.spool import Spool as RealSpool
    from watchdog.server import WatchdogServer
    from watchdog.state import WatchdogState

    spool_path = tmp_path / "spool.db"
    state_path = tmp_path / "watchdog.db"

    monkeypatch.setattr(wd_main, "Spool", lambda: RealSpool(spool_path))
    monkeypatch.setattr(wd_main, "WatchdogState", lambda: WatchdogState(state_path))

    class QuietDB:
        """No chat.db here — the point is the connection ownership, not reading."""

        def messages_after(self, cursor, limit=200):
            return []

        def max_rowid(self):
            return 7

        def close(self):
            pass

    def build():
        db, state, spool = QuietDB(), WatchdogState(state_path), RealSpool(spool_path)
        state.seed_cursor(db.max_rowid())
        return WatchdogServer(wd_main.config, db, state, spool, echo=False), [db, state, spool]

    monkeypatch.setattr(wd_main, "loop", BackgroundLoop("watchdog-test", build))

    with TestClient(wd_main.app) as client:
        body = client.get("/status")
        assert body.status_code == 200, body.text
        assert body.json()["cursor"] == 7


# ---- background lookups ----------------------------------------------------


def test_supervisor_jobs_lists_what_was_promised(supervisor_client, isolated_jobs):
    from supervisor_agent.jobs import DELIVERED

    with isolated_jobs() as store:
        job = store.enqueue("chat-a", "food", "dinner for three", is_group=True)
        store.claim("w")
        store.ready(job.id, "long scraped findings", "el farolito")
        store.settle(job.id, DELIVERED, "send_to_chat")

    body = supervisor_client.get("/jobs")
    assert body.status_code == 200, body.text
    rows = body.json()["jobs"]
    assert len(rows) == 1
    assert rows[0]["job"] == job.job_key
    assert rows[0]["state"] == "delivered"
    assert rows[0]["reply"] == "el farolito"
    assert "findings" not in rows[0], "scraped page content does not belong in an API listing"


def test_supervisor_jobs_hides_the_chat_identifier(supervisor_client, isolated_jobs):
    from plug.events import anon

    with isolated_jobs() as store:
        store.enqueue("chat-a", "food", "dinner")

    rows = supervisor_client.get("/jobs").json()["jobs"]
    assert rows[0]["chat"] == anon("chat-a")
    assert "chat-a" not in str(rows)


def test_supervisor_jobs_can_be_scoped_to_one_chat(supervisor_client, isolated_jobs):
    from plug.events import anon

    with isolated_jobs() as store:
        store.enqueue("chat-a", "food", "first")
        store.enqueue("chat-b", "food", "second")

    rows = supervisor_client.get(f"/jobs?chat={anon('chat-a')}").json()["jobs"]
    assert [r["objective"] for r in rows] == ["first"]
    assert supervisor_client.get("/jobs?chat=nope").status_code == 404


def test_supervisor_status_reports_worker_state(supervisor_client, isolated_jobs):
    body = supervisor_client.get("/status").json()
    assert body["jobs"]["queued"] == 0
    assert body["workers"]["enabled"] is True
    assert body["workers"]["kinds"] == ["food"]
    assert "jobs" in body["paths"]
    assert isinstance(body["workers"]["web_access"], bool)
    assert "token" not in str(body).lower() or "BRIGHTDATA" not in str(body)


def test_supervisor_metrics_include_jobs(supervisor_client, isolated_jobs):
    text = supervisor_client.get("/metrics").text
    assert "plug_jobs_queued" in text
    assert "plug_supervisor_follow_ups_total" in text
