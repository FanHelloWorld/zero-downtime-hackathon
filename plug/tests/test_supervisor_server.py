"""Supervisor server: lease from the pool, decide, reply, acknowledge."""

from __future__ import annotations

from dataclasses import asdict

from supervisor_agent.agent import AgentOutcome
from supervisor_agent.server import SupervisorServer, SupervisorStats
from plug.safety import ReplyPolicy, pause, resume

import pytest

from .conftest import imported_roots, make_message


class StubAgent:
    dry_run = True

    def __init__(self, outcome: AgentOutcome | None = None):
        self.calls: list = []
        self._outcome = outcome or AgentOutcome(sent=True, text="ok")

    def handle(self, batch):
        self.calls.append(batch)
        return self._outcome


@pytest.fixture(autouse=True)
def _unpaused():
    resume()
    yield
    resume()


def build(config, spool, memory, agent) -> SupervisorServer:
    config.supervisor.debounce_seconds = 0  # release immediately in tests
    policy = ReplyPolicy(config, memory)
    return SupervisorServer(config, spool, memory, policy, agent, echo=False, owner="test")


def test_stats_are_serializable():
    assert asdict(SupervisorStats(sent=3))["sent"] == 3


def test_drains_the_pool_and_acknowledges(config, spool, memory):
    spool.enqueue([make_message(rowid=1)])
    agent = StubAgent()
    server = build(config, spool, memory, agent)

    server.tick()

    assert len(agent.calls) == 1
    assert spool.stats().done == 1
    assert spool.stats().depth == 0


def test_burst_becomes_one_reply(config, spool, memory):
    spool.enqueue([make_message(rowid=i, body=f"m{i}") for i in range(3)])
    agent = StubAgent()
    server = build(config, spool, memory, agent)

    server.tick()

    assert len(agent.calls) == 1, "three rapid messages must produce one reply"
    assert len(agent.calls[0].messages) == 3
    assert spool.stats().done == 3, "every pooled item in the batch must be acked"


def test_policy_rejections_are_dropped_not_retried(config, spool, memory):
    spool.enqueue([make_message(rowid=1, handle="262966")])  # short code
    agent = StubAgent()
    server = build(config, spool, memory, agent)

    server.tick()

    assert agent.calls == [], "filtered work must never reach the model"
    assert spool.stats().dropped == 1
    assert spool.stats().pending == 0


def test_agent_skip_is_terminal(config, spool, memory):
    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, StubAgent(AgentOutcome(skipped=True, reason="spam")))

    server.tick()

    assert spool.stats().dropped == 1
    assert server.stats.skipped == 1


def test_blocked_reply_is_terminal(config, spool, memory):
    """A rate-limited reply delivered an hour late reads worse than none."""
    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, StubAgent(AgentOutcome(blocked=True, reason="rate limit")))

    server.tick()

    assert spool.stats().dropped == 1
    assert spool.stats().pending == 0


def test_agent_errors_return_work_to_the_pool(config, spool, memory):
    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, StubAgent(AgentOutcome(errors=["overloaded"])))

    server.tick()

    assert spool.stats().pending == 1, "a transient failure must be retryable"
    assert server.stats.retried == 1


def test_repeated_errors_dead_letter(config, spool, memory):
    config.supervisor.max_attempts = 2
    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, StubAgent(AgentOutcome(errors=["overloaded"])))

    for _ in range(3):
        server.tick()

    assert spool.stats().dead == 1
    assert spool.stats().pending == 0


def test_kill_switch_still_lets_work_be_leased_and_settled(config, spool, memory):
    """Pausing must not wedge the pool — items resolve as blocked, not stuck."""
    spool.enqueue([make_message(rowid=1)])
    pause()
    server = build(config, spool, memory, StubAgent(AgentOutcome(blocked=True, reason="paused")))

    server.tick()

    assert spool.stats().depth == 0


def test_shutdown_returns_buffered_work_to_the_pool(config, spool, memory):
    config.supervisor.debounce_seconds = 999  # nothing will be released
    spool.enqueue([make_message(rowid=1)])
    policy = ReplyPolicy(config, memory)
    server = SupervisorServer(config, spool, memory, policy, StubAgent(), echo=False, owner="test")

    server.tick()
    assert spool.stats().leased == 1

    server._shutdown()
    assert spool.stats().pending == 1, "buffered work must go back, not wait for lease expiry"


def test_empty_pool_is_a_quiet_noop(config, spool, memory):
    agent = StubAgent()
    server = build(config, spool, memory, agent)
    assert server.tick() is False
    assert agent.calls == []


def test_supervisor_never_imports_the_watchdog():
    """It needs Automation permission and an API key — not Full Disk Access.

    Independence is an import-graph property, so that is what gets asserted.
    """
    import supervisor_agent.server as mod

    assert "watchdog" not in imported_roots(mod)


def test_permanent_api_failures_are_not_retried(config, spool, memory):
    """A billing or auth 400 fails identically on every attempt.

    Retrying burns the attempt budget and delays the dead-letter that tells you
    what is actually wrong, so these settle immediately.
    """
    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, StubAgent(
        AgentOutcome(errors=["credit balance is too low"], permanent=True)
    ))

    server.tick()

    assert spool.stats().pending == 0, "a permanent failure must not be requeued"
    assert spool.stats().dropped == 1
    assert server.stats.failed == 1
    assert server.stats.retried == 0


def test_billing_and_auth_errors_are_classified_permanent():
    """Guards the classification itself, not just the branch that uses it."""
    import anthropic

    from supervisor_agent.agent import PERMANENT_API_ERRORS

    assert anthropic.BadRequestError in PERMANENT_API_ERRORS       # includes no-credit
    assert anthropic.AuthenticationError in PERMANENT_API_ERRORS
    assert anthropic.NotFoundError in PERMANENT_API_ERRORS
    # Rate limits and server errors are worth another try.
    assert anthropic.RateLimitError not in PERMANENT_API_ERRORS
    assert anthropic.InternalServerError not in PERMANENT_API_ERRORS


# ---- dry run vs live: does AppleScript actually get invoked? ---------------


def _run_agent(config, memory, *, dry_run, monkeypatch, reply="sure, see you then"):
    """Drive Agent.handle end to end with a fake tool runner that calls send_reply.

    The send tool is a closure built inside handle(), so the only honest way to
    test it is to invoke it the way the SDK runner would.
    """
    import supervisor_agent.agent as agent_mod
    from plug.models import Batch
    from plug.safety import ReplyPolicy

    from .conftest import make_message

    class FakeRunner:
        def __init__(self, tools):
            self._tools = tools

        def __iter__(self):
            send_tool = next(t for t in self._tools if "send" in getattr(t, "name", str(t)))
            send_tool.call({"text": reply}) if hasattr(send_tool, "call") else send_tool(text=reply)
            return iter(())

    class FakeMessages:
        def tool_runner(self, *, tools, **_):
            return FakeRunner(tools)

    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.config, agent.memory = config, memory
    agent.safety, agent.dry_run = ReplyPolicy(config, memory), dry_run
    agent.client = type("C", (), {"beta": type("B", (), {"messages": FakeMessages()})()})()

    batch = Batch(chat_guid="chat-x", messages=[make_message()])
    return agent.handle(batch)


def test_dry_run_never_invokes_applescript(config, memory, monkeypatch):
    """A suppressed send once looked identical to a broken one in the log."""
    import supervisor_agent.agent as agent_mod

    calls = []
    monkeypatch.setattr(agent_mod.send, "deliver", lambda *a, **k: calls.append(a))

    outcome = _run_agent(config, memory, dry_run=True, monkeypatch=monkeypatch)

    assert calls == [], "dry run must not reach send.deliver / osascript"
    assert outcome.sent
    assert outcome.strategy == "dry-run"


def test_live_mode_does_invoke_applescript(config, memory, monkeypatch):
    """The inverse, and the thing that was actually 'broken': with dry run off,
    the send tool must call through to the AppleScript sender."""
    import supervisor_agent.agent as agent_mod
    from supervisor_agent.send import SendResult

    calls = []

    def fake_deliver(text, chat_guid, handle, service):
        calls.append((text, chat_guid, service))
        return SendResult("send_to_chat", True)

    monkeypatch.setattr(agent_mod.send, "deliver", fake_deliver)

    outcome = _run_agent(config, memory, dry_run=False, monkeypatch=monkeypatch)

    assert len(calls) == 1, "live mode must reach the AppleScript sender"
    assert calls[0][0] == "sure, see you then"
    assert outcome.sent and outcome.strategy == "send_to_chat"


def test_dry_run_sends_are_not_counted_against_live_rate_limits(config, memory, monkeypatch):
    import supervisor_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod.send, "deliver", lambda *a, **k: None)
    _run_agent(config, memory, dry_run=True, monkeypatch=monkeypatch)

    assert memory.sends_in_last_hour("chat-x") == 0
