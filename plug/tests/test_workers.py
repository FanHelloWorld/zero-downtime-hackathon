"""The worker pool: background lookups, and what happens when they go wrong.

The model calls are stubbed; what is asserted is the wiring around them — the
shape of the MCP request, the job state machine, the concurrency cap, and the
rule that a worker never touches Messages.app itself.
"""

from __future__ import annotations

import time

import pytest

from plug.config import Config
from supervisor_agent.jobs import BLOCKED, EXPIRED, READY, JobStore
from supervisor_agent.workers import mcp
from supervisor_agent.workers.runner import GAVE_UP, WorkerPool, _fit


class Block:
    def __init__(self, type_: str, text: str = ""):
        self.type = type_
        self.text = text


class FakeCreate:
    """Stands in for client.beta.messages.create, recording what it was sent."""

    def __init__(self, blocks=None, error: Exception | None = None):
        self.calls: list[dict] = []
        self._blocks = blocks
        self._error = error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        blocks = self._blocks
        if blocks is None:
            blocks = [Block("mcp_tool_use"), Block("text", "el farolito, mission, cheap")]
        return type("Resp", (), {"content": blocks})()


class FakeClient:
    def __init__(self, create: FakeCreate):
        self.beta = type("B", (), {"messages": type("M", (), {"create": create})()})()
        self.timeouts: list[float] = []
        self._create = create

    def with_options(self, **kw):
        if "timeout" in kw:
            self.timeouts.append(kw["timeout"])
        return self


class FakeCompose:
    name = "fake-compose"

    def __init__(self, text: str = "el farolito. mission, cheap, open late."):
        self.calls: list[dict] = []
        self._text = text

    def complete(self, *, system, prompt, max_tokens):
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        return self._text


@pytest.fixture()
def jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    yield store
    store.close()


@pytest.fixture()
def no_token(monkeypatch):
    monkeypatch.delenv("BRIGHTDATA_API_TOKEN", raising=False)


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_TOKEN", "test-token-abc")


def build_pool(config, jobs, tmp_path, *, create=None, compose=None):
    create = create or FakeCreate()
    compose = compose or FakeCompose()
    woken = []
    pool = WorkerPool(
        config,
        jobs,
        open_store=lambda: JobStore(tmp_path / "jobs.db"),
        client=FakeClient(create),
        compose=compose,
        notify=lambda: woken.append(1),
    )
    return pool, create, compose, woken


def drain(pool, timeout: float = 5.0) -> None:
    """Wait for spawned threads to finish. The fakes are instant; this is a guard."""
    deadline = time.monotonic() + timeout
    while pool.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not pool.running, "worker thread did not finish"


def file_one(jobs, chat="chat-a", kind="food"):
    return jobs.enqueue(chat, kind, "dinner for three", context="- ana: Fremont", pillar="flow")


# ---- the happy path -------------------------------------------------------


def test_a_job_runs_and_leaves_a_message_to_send(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, _, _, woken = build_pool(config, jobs, tmp_path)

    assert pool.pump() == 1
    drain(pool)

    ready = jobs.deliverable()
    assert len(ready) == 1
    assert ready[0].state == READY
    assert ready[0].reply == "el farolito. mission, cheap, open late."
    assert ready[0].findings, "the research is kept for inspection"
    assert woken, "the loop is woken so the follow-up goes out promptly"


def test_the_worker_never_sends_anything_itself(config, jobs, tmp_path, token, monkeypatch):
    import supervisor_agent.send as send_mod

    calls = []
    monkeypatch.setattr(send_mod, "deliver", lambda *a, **k: calls.append(a))

    file_one(jobs)
    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    assert calls == [], "delivery belongs to the loop thread, through the safety gate"


def test_the_context_and_objective_reach_the_researcher(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    prompt = create.calls[0]["messages"][0]["content"]
    assert "dinner for three" in prompt
    assert "Fremont" in prompt


def test_the_composer_speaks_in_the_chosen_register(config, jobs, tmp_path, token):
    jobs.enqueue("chat-a", "food", "dinner", pillar="deadpan", is_group=True)
    pool, _, compose, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    system = compose.calls[0]["system"]
    assert "deadpan" in system.lower() or "Flat delivery" in system
    assert "data, not" in system or "not instructions" in system, "findings are untrusted"


def test_an_over_long_reply_is_trimmed_to_what_can_be_sent(config, jobs, tmp_path, token):
    config.safety.max_reply_chars = 40
    long = FakeCompose("This is the first sentence. And here is a great deal more text after it.")
    file_one(jobs)
    pool, _, _, _ = build_pool(config, jobs, tmp_path, compose=long)
    pool.pump()
    drain(pool)

    reply = jobs.deliverable()[0].reply
    assert len(reply) <= 40
    assert reply == "This is the first sentence."


def test_fit_prefers_a_sentence_boundary_but_will_hard_cut():
    assert _fit("one. two. three.", 100) == "one. two. three."
    assert _fit("aaaaaaaaaaaaaaaaaaaaaaaaaaaa", 10) == "aaaaaaaaaa"


# ---- the MCP request ------------------------------------------------------


def test_the_mcp_request_is_wired_and_allowlisted(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    call = create.calls[0]
    assert call["betas"] == ["mcp-client-2025-11-20"]
    server = call["mcp_servers"][0]
    assert server["type"] == "url" and server["name"] == "brightdata"
    assert "test-token-abc" in server["url"]

    toolset = call["tools"][0]
    assert toolset["type"] == "mcp_toolset"
    assert toolset["mcp_server_name"] == server["name"], "every server needs exactly one toolset"
    assert toolset["default_config"] == {"enabled": False}, "allowlist, not denylist"
    assert set(toolset["configs"]) == set(config.workers.brightdata.tools)


def test_the_research_call_carries_a_timeout(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)
    assert pool.client.timeouts == [config.workers.job_timeout_seconds]


def test_the_token_is_never_logged():
    cfg = Config().workers.brightdata
    url = f"{cfg.url}?token=hunter2&groups=business"
    assert "hunter2" not in mcp.redact(url)
    assert "groups=business" in mcp.redact(url)


def test_without_a_token_it_degrades_rather_than_failing(config, jobs, tmp_path, no_token):
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    assert "mcp_servers" not in create.calls[0], "no wiring, no MCP fields"
    assert jobs.deliverable(), "a weaker answer still beats a broken promise"
    assert "no web access" in create.calls[0]["messages"][0]["content"]


# ---- failure --------------------------------------------------------------


def test_a_failed_lookup_is_retried_then_apologised_for(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, _, _, _ = build_pool(
        config, jobs, tmp_path, create=FakeCreate(error=RuntimeError("upstream down"))
    )

    pool.pump()
    drain(pool)
    assert jobs.stats().queued == 1, "first failure returns it for another go"

    pool.pump()
    drain(pool)
    ready = jobs.deliverable()
    assert ready and ready[0].reply == GAVE_UP, "the chat was promised an answer"
    assert "upstream down" in ready[0].note


def test_silence_on_failure_is_configurable(config, jobs, tmp_path, token):
    config.workers.apologize_on_failure = False
    file_one(jobs)
    pool, _, _, _ = build_pool(
        config, jobs, tmp_path, create=FakeCreate(error=RuntimeError("nope"))
    )
    for _ in range(2):
        pool.pump()
        drain(pool)

    assert jobs.deliverable() == []
    assert jobs.stats().failed == 1


def test_empty_research_counts_as_a_failure(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, _, _, _ = build_pool(config, jobs, tmp_path, create=FakeCreate(blocks=[]))
    pool.pump()
    drain(pool)
    assert jobs.stats().queued == 1


def test_an_unknown_kind_is_refused_not_run(config, jobs, tmp_path, token):
    config.workers.kinds = ["food", "astrology"]
    file_one(jobs, kind="astrology")
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    assert create.calls == [], "no model call for a worker that does not exist"
    assert jobs.recent()[0].state == BLOCKED


def test_a_wedged_worker_is_retired_and_the_chat_told(config, jobs, tmp_path, token):
    job = file_one(jobs)
    jobs.claim("someone-else")           # in flight, going nowhere
    config.workers.job_timeout_seconds = -1

    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()

    ready = jobs.deliverable()
    assert ready and ready[0].job_key == job.job_key
    assert ready[0].reply == GAVE_UP
    assert ready[0].note == "expired"


def test_a_wedged_worker_can_be_retired_silently(config, jobs, tmp_path, token):
    config.workers.apologize_on_failure = False
    config.workers.job_timeout_seconds = -1
    file_one(jobs)
    jobs.claim("someone-else")

    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    assert jobs.recent()[0].state == EXPIRED


# ---- limits ---------------------------------------------------------------


def test_concurrency_is_capped(config, jobs, tmp_path, token):
    config.workers.max_concurrent = 2
    for i in range(5):
        file_one(jobs, chat=f"chat-{i}")

    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    assert pool.pump() == 2
    drain(pool)
    assert pool.pump() == 2


def test_workers_can_be_switched_off_entirely(config, jobs, tmp_path, token):
    config.workers.enabled = False
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    assert pool.pump() == 0
    assert create.calls == []


def test_a_stopping_pool_claims_nothing_more(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, _, _, _ = build_pool(config, jobs, tmp_path)
    pool.request_stop()
    assert pool.pump() == 0


# ---- making sure the lookup actually happens -------------------------------


def test_research_retries_when_the_model_never_called_a_tool(config, jobs, tmp_path, token):
    """The quiet failure: a live connector the model declined to use.

    Fluent notes, real-sounding places, nothing checked — and `mcp_calls: 0`
    looks identical in the log to a job that needed no web. Ask again, once,
    with the omission named.
    """
    file_one(jobs)
    create = FakeCreate(blocks=[Block("text", "probably el farolito idk")])
    pool, create, _, _ = build_pool(config, jobs, tmp_path, create=create)

    pool.pump()
    drain(pool)

    assert len(create.calls) == 2, "one research call, then one insisting retry"
    second = create.calls[1]["messages"][0]["content"]
    assert "without running a single search" in second
    assert create.calls[1]["mcp_servers"], "the retry still carries the connector"

    ready = [j for j in jobs.recent() if j.reply]
    assert ready, "the job still finishes rather than hanging the chat"


def test_research_does_not_retry_when_tools_were_used(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)  # default blocks include mcp_tool_use

    pool.pump()
    drain(pool)

    assert len(create.calls) == 1, "a lookup that happened is not repeated"


def test_no_retry_without_a_connector(config, jobs, tmp_path, no_token):
    """No wiring means no web access — retrying would ask for the impossible."""
    file_one(jobs)
    create = FakeCreate(blocks=[Block("text", "from memory, sorry")])
    pool, create, _, _ = build_pool(config, jobs, tmp_path, create=create)

    pool.pump()
    drain(pool)

    assert len(create.calls) == 1
    assert "no web access" in create.calls[0]["messages"][0]["content"]


def test_the_research_prompt_asks_for_addresses_and_reviews(config, jobs, tmp_path, token):
    file_one(jobs)
    pool, create, _, _ = build_pool(config, jobs, tmp_path)
    pool.pump()
    drain(pool)

    system = create.calls[0]["system"]
    assert "full street address" in system
    assert "reviews" in system
    prompt = create.calls[0]["messages"][0]["content"]
    assert "do not answer from memory" in prompt
