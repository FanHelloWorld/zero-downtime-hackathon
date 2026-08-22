"""Delegation: saying something now and filing a job for the real answer.

The judgement — is this worth looking up — belongs to the model. Everything a
wrong judgement could cost is checked here in code: whether the tool exists at
all, whether the chat is over its quota, and whether the holding message clears
the same gate as any other reply.
"""

from __future__ import annotations

import pytest

from plug.mention import MentionDetector
from plug.models import Batch
from plug.safety import ReplyPolicy
from supervisor_agent.agent import Agent
from supervisor_agent.jobs import JobStore, QUEUED
from supervisor_agent.planner import Plan
from supervisor_agent.send import SendResult

from .conftest import make_message
from .test_group import StubPlanner, group_batch


@pytest.fixture()
def jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    yield store
    store.close()


def food_plan(**kwargs):
    kwargs.setdefault("pillar", "flow")
    kwargs.setdefault(
        "action",
        {"kind": "food", "warranted": True, "objective": "somewhere for dinner", "missing": []},
    )
    return Plan(**kwargs)


def build_agent(config, memory, jobs, *, planner=None, dry_run=True, choose="delegate"):
    """An Agent whose model always reaches for one named tool."""
    seen: dict = {}

    class FakeRunner:
        def __init__(self, tools):
            self._tools = tools

        def __iter__(self):
            seen["tools"] = [t.name for t in self._tools]
            picked = next((t for t in self._tools if t.name == choose), None)
            if picked is None:
                return iter(())
            args = {
                "delegate": {
                    "objective": "dinner for three",
                    "holding_text": "hang on, lemme look",
                },
                "send_reply": {"text": "sure"},
                "skip": {"reason": "nothing to add"},
            }[choose]
            picked.call(args)

            return iter(())

    class FakeMessages:
        def tool_runner(self, *, tools, **kw):
            seen["system"] = kw.get("system", "")
            return FakeRunner(tools)

    agent = Agent.__new__(Agent)
    agent.config, agent.memory, agent.jobs = config, memory, jobs
    agent.safety, agent.dry_run = ReplyPolicy(config, memory), dry_run
    agent.client = type("C", (), {"beta": type("B", (), {"messages": FakeMessages()})()})()
    agent.planner = planner or StubPlanner(food_plan())
    agent.mention = MentionDetector(config.group.agent_name, config.group.aliases)
    return agent, seen


# ---- whether the tool exists at all ---------------------------------------


def test_the_tool_is_absent_when_workers_are_off(config, memory, jobs):
    config.workers.enabled = False
    agent, seen = build_agent(config, memory, jobs)
    agent.handle(group_batch("plug where should we eat"))
    assert seen["tools"] == ["send_reply", "skip"]


def test_the_tool_is_absent_without_a_job_store(config, memory):
    agent, seen = build_agent(config, memory, None)
    agent.handle(group_batch("plug where should we eat"))
    assert seen["tools"] == ["send_reply", "skip"]


def test_the_tool_is_absent_when_no_configured_kind_exists(config, memory, jobs):
    config.workers.kinds = ["astrology"]
    agent, seen = build_agent(config, memory, jobs)
    agent.handle(group_batch("plug where should we eat"))
    assert seen["tools"] == ["send_reply", "skip"]


def test_the_tool_is_offered_when_a_worker_could_run(config, memory, jobs):
    agent, seen = build_agent(config, memory, jobs)
    agent.handle(group_batch("plug where should we eat"))
    assert "delegate" in seen["tools"]


def test_a_planner_that_proposes_nothing_still_leaves_the_tool_available(config, memory, jobs):
    """A direct "where should we eat" deserves a lookup even if the room read idle."""
    idle = StubPlanner(food_plan(action={"kind": "none", "warranted": False,
                                         "objective": "", "missing": []}))
    agent, seen = build_agent(config, memory, jobs, planner=idle)
    agent.handle(group_batch("plug where should we eat"))
    assert "delegate" in seen["tools"]


# ---- what delegating actually does ----------------------------------------


def test_delegating_files_a_job_and_says_something_now(config, memory, jobs):
    agent, _ = build_agent(config, memory, jobs, dry_run=True)
    outcome = agent.handle(group_batch("plug where should we eat"))

    assert outcome.delegated and outcome.job_key
    filed = jobs.recent()
    assert len(filed) == 1
    assert filed[0].state == QUEUED
    assert filed[0].kind == "food"
    assert filed[0].objective == "dinner for three"
    assert outcome.text == "hang on, lemme look"


def test_the_job_carries_the_chat_it_came_from(config, memory, jobs):
    """The recipient is closure-bound, exactly like the send tool's."""
    agent, _ = build_agent(config, memory, jobs)
    batch = group_batch("plug where should we eat")
    agent.handle(batch)
    assert jobs.recent()[0].chat_guid == batch.chat_guid
    assert jobs.recent()[0].is_group is True


def test_dry_run_files_the_job_but_sends_nothing(config, memory, jobs, monkeypatch):
    import supervisor_agent.agent as agent_mod

    calls = []
    monkeypatch.setattr(agent_mod.send, "deliver", lambda *a, **k: calls.append(a))

    agent, _ = build_agent(config, memory, jobs, dry_run=True)
    agent.handle(group_batch("plug where should we eat"))

    assert calls == [], "dry run must not reach AppleScript"
    assert len(jobs.recent()) == 1, "but the worker still runs, so the demo is checkable"
    assert memory.sends_in_last_hour() == 0


def test_live_delegation_sends_the_holding_message(config, memory, jobs, monkeypatch):
    import supervisor_agent.agent as agent_mod

    sent = []
    monkeypatch.setattr(
        agent_mod.send, "deliver",
        lambda text, guid, handle, service: sent.append(text) or SendResult("send_to_chat", True),
    )

    agent, _ = build_agent(config, memory, jobs, dry_run=False)
    outcome = agent.handle(group_batch("plug where should we eat"))

    assert sent == ["hang on, lemme look"]
    assert outcome.delegated
    assert memory.sends_in_last_hour() == 1, "the holding message costs a send"


def test_a_holding_message_that_cannot_be_sent_cancels_the_promise(config, memory, jobs, monkeypatch):
    import supervisor_agent.agent as agent_mod

    def explode(*a, **k):
        raise agent_mod.send.SendError("Messages is wedged")

    monkeypatch.setattr(agent_mod.send, "deliver", explode)

    agent, _ = build_agent(config, memory, jobs, dry_run=False)
    outcome = agent.handle(group_batch("plug where should we eat"))

    assert not outcome.delegated
    assert jobs.deliverable() == []
    assert jobs.active_for_chat(jobs.recent()[0].chat_guid) == 0, "nothing said, nothing owed"


# ---- the checks the prompt cannot enforce ---------------------------------


def test_a_chat_cannot_have_two_lookups_running(config, memory, jobs):
    agent, _ = build_agent(config, memory, jobs)
    batch = group_batch("plug where should we eat")
    agent.handle(batch)
    agent.handle(batch)
    assert len(jobs.recent()) == 1


def test_the_cooldown_holds_after_a_job_settles(config, memory, jobs):
    from supervisor_agent.jobs import DELIVERED

    agent, _ = build_agent(config, memory, jobs)
    batch = group_batch("plug where should we eat")
    agent.handle(batch)
    jobs.settle(jobs.recent()[0].id, DELIVERED, "done")

    config.workers.per_chat_cooldown_seconds = 3600
    agent.handle(batch)
    assert len(jobs.recent()) == 1, "back-to-back lookups are the failure mode"


def test_the_daily_quota_is_enforced_in_code(config, memory, jobs):
    from supervisor_agent.jobs import DELIVERED

    config.workers.per_chat_cooldown_seconds = 0
    config.workers.max_per_chat_per_day = 2

    agent, _ = build_agent(config, memory, jobs)
    batch = group_batch("plug where should we eat")
    for _ in range(4):
        agent.handle(batch)
        for job in jobs.deliverable() or jobs.recent(limit=1):
            jobs.settle(job.id, DELIVERED, "done")
    assert len(jobs.recent()) == 2


def test_the_kill_switch_stops_a_delegation_before_it_starts(config, memory, jobs):
    from plug.safety import pause, resume

    pause()
    try:
        agent, _ = build_agent(config, memory, jobs, dry_run=False)
        outcome = agent.handle(group_batch("plug where should we eat"))
        assert not outcome.delegated
        assert outcome.blocked
        assert jobs.recent() == [], "no promise is filed if we cannot speak"
    finally:
        resume()


def test_the_rate_limit_stops_a_delegation(config, memory, jobs):
    config.safety.per_chat_per_hour = 1
    batch = group_batch("plug where should we eat")
    memory.record_send(batch.chat_guid, "earlier reply", dry_run=False)

    agent, _ = build_agent(config, memory, jobs, dry_run=False)
    # The loop guard would also catch this; clear it so the rate limit is what bites.
    config.safety.loop_window_seconds = 0
    outcome = agent.handle(batch)

    assert not outcome.delegated and outcome.blocked
    assert jobs.recent() == []


# ---- context handed on -----------------------------------------------------


def test_the_dossier_travels_with_the_job(config, memory, jobs):
    facts = [
        {"who": "+1aaa", "location": "1234 Oak St, Fremont, CA", "availability": "after 7"},
        {"who": "+1bbb", "location": "Oakland", "note": "vegetarian"},
    ]
    planner = StubPlanner(food_plan(facts_learned=facts, tone="hungry"))
    agent, _ = build_agent(config, memory, jobs, planner=planner)
    agent.handle(group_batch("plug where should we eat"))

    context = jobs.recent()[0].context
    assert "Fremont" in context and "Oakland" in context
    assert "after 7" in context and "vegetarian" in context
    assert "1234" not in context, "street numbers must not leave this machine"


def test_exact_locations_are_opt_in(config, memory, jobs):
    config.workers.share_locations = "exact"
    facts = [{"who": "+1aaa", "location": "1234 Oak St, Fremont, CA"}]
    planner = StubPlanner(food_plan(facts_learned=facts))
    agent, _ = build_agent(config, memory, jobs, planner=planner)
    agent.handle(group_batch("plug where should we eat"))
    assert "1234" in jobs.recent()[0].context


def test_the_dossier_persists_across_bursts(config, memory, jobs):
    """What Tuesday taught us has to still be true on Friday.

    Two agents rather than one, sharing the stores: the dossier has to survive a
    restart, not merely live in a long-running object.
    """
    tuesday = StubPlanner(food_plan(facts_learned=[{"who": "+1aaa", "location": "Fremont"}]))
    early, _ = build_agent(config, memory, jobs, planner=tuesday, choose="skip")
    early.handle(group_batch("plug hey"))

    friday = StubPlanner(food_plan(facts_learned=[{"who": "+1bbb", "availability": "8pm"}]))
    later, _ = build_agent(config, memory, jobs, planner=friday, choose="delegate")
    later.handle(group_batch("plug where should we eat"))

    context = jobs.recent()[0].context

    assert "Fremont" in context, "a later burst must not erase an earlier fact"
    assert "8pm" in context


def test_one_to_one_can_delegate_too(config, memory, jobs):
    agent, seen = build_agent(config, memory, jobs)
    outcome = agent.handle(Batch(chat_guid="c", messages=[make_message(body="where should i eat")]))
    assert "delegate" in seen["tools"]
    assert outcome.delegated
    assert jobs.recent()[0].is_group is False
