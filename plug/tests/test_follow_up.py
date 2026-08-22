"""Delivering what a worker found.

The follow-up is the only message this system sends that nobody asked for in the
moment — the request came minutes earlier. So it goes out from the loop thread
through the same gate as any reply, with exactly one exemption, and this is where
that exemption is pinned down.
"""

from __future__ import annotations

import pytest

from plug.safety import ReplyPolicy, pause, resume
from supervisor_agent.agent import AgentOutcome
from supervisor_agent.jobs import BLOCKED, DELIVERED, JobStore
from supervisor_agent.send import SendResult
from supervisor_agent.server import SupervisorServer

from .conftest import make_message


class StubAgent:
    def __init__(self, dry_run: bool = False, outcome: AgentOutcome | None = None):
        self.dry_run = dry_run
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


@pytest.fixture()
def jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    yield store
    store.close()


@pytest.fixture()
def sent(monkeypatch):
    """Capture AppleScript sends without invoking osascript."""
    import supervisor_agent.server as server_mod

    calls: list[str] = []
    monkeypatch.setattr(
        server_mod.send, "deliver",
        lambda text, guid, handle, service: calls.append(text) or SendResult("send_to_chat", True),
    )
    return calls


def build(config, spool, memory, jobs, *, dry_run=False, agent=None):
    config.supervisor.debounce_seconds = 0
    policy = ReplyPolicy(config, memory)
    agent = agent or StubAgent(dry_run=dry_run)
    return SupervisorServer(
        config, spool, memory, policy, agent, echo=False, owner="test", jobs=jobs
    )


def ready_job(jobs, chat="chat-a", reply="el farolito, it's the one", is_group=True):
    job = jobs.enqueue(chat, "food", "dinner", is_group=is_group, handle="+1aaa")
    jobs.claim("w")
    jobs.ready(job.id, "findings", reply)
    return job


# ---- the ordinary case ----------------------------------------------------


def test_a_ready_job_is_delivered_and_settled(config, spool, memory, jobs, sent):
    job = ready_job(jobs)
    server = build(config, spool, memory, jobs)

    assert server.tick() is True
    assert sent == ["el farolito, it's the one"]
    assert jobs.recent()[0].state == DELIVERED
    assert server.stats.follow_ups == 1


def test_the_follow_up_is_remembered_as_ours(config, spool, memory, jobs, sent):
    ready_job(jobs)
    build(config, spool, memory, jobs).tick()

    assert memory.sends_in_last_hour("chat-a") == 1, "it counts against the rate limit"
    history = memory.recent_history("chat-a", 5)
    assert history[-1] == {"role": "assistant", "content": "el farolito, it's the one"}


def test_nothing_to_deliver_is_not_busy_work(config, spool, memory, jobs, sent):
    server = build(config, spool, memory, jobs)
    assert server.tick() is False
    assert sent == []


# ---- the one exemption ----------------------------------------------------


def test_the_loop_guard_does_not_swallow_a_promised_answer(config, spool, memory, jobs, sent):
    """The holding message was sent seconds ago; the guard would block the answer.

    That guard exists to stop two bots ping-ponging. A follow-up is the second
    half of one human request, so it is the one thing allowed through.
    """
    config.safety.loop_window_seconds = 3600
    memory.record_send("chat-a", "hang on, lemme look", dry_run=False)
    ready_job(jobs)

    build(config, spool, memory, jobs).tick()
    assert sent == ["el farolito, it's the one"]


def test_the_exemption_is_only_the_loop_guard(config, spool, memory, jobs, sent):
    """Everything else still bites — otherwise it would be a hole, not an exemption."""
    config.safety.per_chat_per_hour = 1
    config.safety.loop_window_seconds = 3600
    memory.record_send("chat-a", "hang on", dry_run=False)
    ready_job(jobs)

    server = build(config, spool, memory, jobs)
    server.tick()

    assert sent == []
    assert jobs.recent()[0].state == BLOCKED
    assert "rate limit" in jobs.recent()[0].note
    assert server.stats.follow_ups_blocked == 1


def test_the_kill_switch_stops_a_follow_up(config, spool, memory, jobs, sent):
    ready_job(jobs)
    server = build(config, spool, memory, jobs)
    pause()
    try:
        server.tick()
    finally:
        resume()

    assert sent == []
    assert jobs.recent()[0].state == BLOCKED
    assert "paused" in jobs.recent()[0].note


def test_an_over_long_follow_up_is_refused(config, spool, memory, jobs, sent):
    config.safety.max_reply_chars = 10
    ready_job(jobs, reply="x" * 50)
    build(config, spool, memory, jobs).tick()

    assert sent == []
    assert jobs.recent()[0].state == BLOCKED


# ---- dry run --------------------------------------------------------------


def test_dry_run_drafts_the_follow_up_without_sending(config, spool, memory, jobs, sent):
    ready_job(jobs)
    server = build(config, spool, memory, jobs, dry_run=True)
    server.tick()

    assert sent == [], "AppleScript must not be reached"
    assert jobs.recent()[0].state == DELIVERED
    assert memory.sends_in_last_hour("chat-a") == 0, "a dry run costs no rate limit"


# ---- failure --------------------------------------------------------------


def test_a_send_failure_settles_rather_than_spinning(config, spool, memory, jobs, monkeypatch):
    import supervisor_agent.server as server_mod

    def explode(*a, **k):
        raise server_mod.send.SendError("every strategy failed")

    monkeypatch.setattr(server_mod.send, "deliver", explode)

    ready_job(jobs)
    server = build(config, spool, memory, jobs)
    server.tick()

    assert jobs.recent()[0].state == BLOCKED
    assert jobs.deliverable() == [], "retrying each tick would spin, not recover"
    assert server.stats.follow_ups_blocked == 1


# ---- the pool item that started it ----------------------------------------


def test_a_delegated_batch_is_settled_immediately(config, spool, memory, jobs, sent):
    """The promise now lives in the job store; retrying the batch would double it."""
    spool.enqueue([make_message(rowid=1)])
    agent = StubAgent(outcome=AgentOutcome(delegated=True, job_key="j_abc", text="hang on"))
    server = build(config, spool, memory, jobs, agent=agent)

    server.tick()

    assert spool.stats().done == 1
    assert spool.stats().depth == 0
    assert server.stats.delegated == 1


# ---- shutdown -------------------------------------------------------------


def test_shutdown_hands_running_jobs_back(config, spool, memory, jobs):
    from supervisor_agent.workers import WorkerPool

    jobs.enqueue("chat-a", "food", "dinner")
    server = build(config, spool, memory, jobs)
    server.workers = WorkerPool(config, jobs, owner="pool-owner", client=object())
    jobs.claim("pool-owner")

    server._shutdown()

    assert jobs.stats().queued == 1, "an interrupted lookup is retried, not stranded"


def test_a_worker_pool_that_misbehaves_does_not_stop_replies(config, spool, memory, jobs):
    class Exploding:
        owner = "x"

        def pump(self):
            raise RuntimeError("pool is unwell")

        def request_stop(self):
            pass

    spool.enqueue([make_message(rowid=1)])
    server = build(config, spool, memory, jobs)
    server.workers = Exploding()

    server.tick()

    assert spool.stats().done == 1, "the reply path is independent of the worker pool"
