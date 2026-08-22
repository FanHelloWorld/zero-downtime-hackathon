"""The watchdog → supervisor alert.

Notification is an optimisation layered on top of polling, so these tests cover
both halves: that the ping actually lands, and that losing it degrades to a
slower pickup rather than a broken watchdog.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from plug.notify import Notifier


class _Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        type(self).received.append(json.loads(body) if body else {})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_):  # silence the default stderr logging
        pass


@pytest.fixture()
def listener():
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/notify", _Recorder
    server.shutdown()


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_ping_reaches_the_supervisor(listener):
    url, recorder = listener
    Notifier(url).ping(3)

    assert _wait_for(lambda: recorder.received)
    assert recorder.received[0] == {"spooled": 3}


def test_ping_does_not_block_the_poll_loop(listener):
    """It runs on its own thread; the watchdog must never wait on the agent."""
    url, _ = listener
    started = time.monotonic()
    Notifier(url).ping(1)
    assert time.monotonic() - started < 0.5


def test_disabled_when_no_url_is_configured():
    notifier = Notifier(None)
    assert not notifier.enabled
    notifier.ping(1)  # must be a silent no-op


def test_a_dead_supervisor_does_not_break_the_watchdog():
    """Connection refused is expected whenever the agent is stopped.

    The watchdog has to keep pooling regardless — that independence is the whole
    point of the split.
    """
    # Port 1 is reserved and never listening.
    notifier = Notifier("http://127.0.0.1:1/notify", timeout=0.5)
    notifier.ping(1)  # must not raise

    assert _wait_for(lambda: notifier._failures >= 1, timeout=5)


def test_pings_are_coalesced_while_one_is_in_flight(listener):
    """One wake-up drains everything pending, so a burst needs only one ping."""
    url, recorder = listener
    notifier = Notifier(url)
    for _ in range(20):
        notifier.ping(1)

    time.sleep(0.5)
    assert 1 <= len(recorder.received) < 20


# ---- integration with the two servers -------------------------------------


def test_watchdog_pings_after_pooling(config, tmp_path, spool):
    from watchdog.server import WatchdogServer
    from watchdog.state import WatchdogState

    from .conftest import make_message

    class SpyNotifier:
        def __init__(self):
            self.pings = []
            self.url = None
            self.enabled = False

        def ping(self, count):
            self.pings.append(count)

    class StubDB:
        def __init__(self, ticks):
            self._ticks = ticks

        def messages_after(self, cursor, limit=200):
            return self._ticks.pop(0) if self._ticks else []

        def max_rowid(self):
            return 0

    spy = SpyNotifier()
    state = WatchdogState(tmp_path / "watchdog.db")
    state.set_cursor(0)
    server = WatchdogServer(
        config, StubDB([[make_message(rowid=1), make_message(rowid=2)], []]),
        state, spool, echo=False, notifier=spy,
    )

    server.tick()
    assert spy.pings == [2], "the agent must be alerted as soon as work is pooled"

    server.tick()
    assert spy.pings == [2], "an empty tick must not ping"


def test_supervisor_notify_wakes_the_loop(config, spool, memory):
    """Without this the loop sits out idle_sleep_seconds before noticing."""
    from plug.safety import ReplyPolicy
    from supervisor_agent.server import SupervisorServer

    class StubAgent:
        dry_run = True

        def handle(self, batch):
            from supervisor_agent.agent import AgentOutcome

            return AgentOutcome(sent=True)

    config.supervisor.idle_sleep_seconds = 60  # long enough that polling can't explain it
    server = SupervisorServer(
        config, spool, memory, ReplyPolicy(config, memory), StubAgent(), echo=False, owner="t"
    )

    assert not server._wake.is_set()
    server.notify()
    assert server._wake.is_set(), "notify must release the loop's wait immediately"


def test_request_stop_also_wakes_the_loop(config, spool, memory):
    """Otherwise shutdown waits out a full idle interval."""
    from plug.safety import ReplyPolicy
    from supervisor_agent.server import SupervisorServer

    server = SupervisorServer(
        config, spool, memory, ReplyPolicy(config, memory), object(), echo=False, owner="t"
    )
    server.request_stop()
    assert server._wake.is_set()


def test_notify_endpoint_wakes_the_server(monkeypatch, tmp_path):
    import supervisor_agent.main as sup_main
    from fastapi.testclient import TestClient
    from supervisor_agent.memory import Memory
    from plug.spool import Spool

    from .test_http import StubBackgroundLoop

    class Wakeable:
        owner = "t"

        def __init__(self):
            self.woken = 0

        def notify(self):
            self.woken += 1

    inner = Wakeable()
    monkeypatch.setattr(sup_main, "loop", StubBackgroundLoop(inner))
    monkeypatch.setattr(sup_main, "Spool", lambda: Spool(tmp_path / "spool.db"))
    monkeypatch.setattr(sup_main, "Memory", lambda: Memory(tmp_path / "supervisor.db"))

    with TestClient(sup_main.app) as client:
        assert client.post("/notify").json() == {"woken": True}

    assert inner.woken == 1
