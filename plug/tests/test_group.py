"""Group-chat behaviour: read the room always, speak only when addressed.

The interesting parts of this feature are judgement calls a test can't assert,
so these pin the mechanical guarantees around them — above all that the agent
stays quiet unless a human actually named it.
"""

from __future__ import annotations

import time

import pytest

from plug.mention import MentionDetector
from plug.models import STYLE_GROUP, Batch
from plug.safety import ReplyPolicy
from supervisor_agent import personality
from supervisor_agent.agent import Agent, AgentOutcome
from supervisor_agent.planner import Plan

from .conftest import make_message


def group_message(**kwargs):
    kwargs.setdefault("chat_guid", "any;+;groupchat")
    kwargs.setdefault("style", STYLE_GROUP)
    return make_message(**kwargs)


def group_batch(*bodies, handles=None, participants=("+1aaa", "+1bbb", "+1ccc")):
    handles = handles or ["+1aaa"] * len(bodies)
    messages = [
        group_message(rowid=i, body=b, handle=h, participants=participants)
        for i, (b, h) in enumerate(zip(bodies, handles))
    ]
    return Batch(chat_guid=messages[0].chat_guid, messages=messages)


# ---- mention detection ----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["hey plug what do you think", "@plug thoughts?", "Plug, help", "plug?", "PLUG!!"],
)
def test_detects_being_named(body):
    assert MentionDetector("plug").in_message(group_message(body=body))


@pytest.mark.parametrize(
    "body",
    [
        "i unplugged the router",
        "try the plugin",
        "plug-in adapter",
        "replugged it",
        "deep in the plugosphere",
        "no mention at all",
    ],
)
def test_does_not_fire_on_lookalikes(body):
    """Substring matching here would make the agent interrupt constantly."""
    assert not MentionDetector("plug").in_message(group_message(body=body))


def test_tag_records_who_and_why():
    tag = MentionDetector("plug").find(group_batch("plug you around?", handles=["+1bbb"]))
    assert tag.by == "+1bbb"
    assert "named" in tag.reason


def test_latest_invitation_wins():
    tag = MentionDetector("plug").find(
        group_batch("plug thoughts?", "actually plug forget it", handles=["+1aaa", "+1bbb"])
    )
    assert tag.text == "actually plug forget it"


def test_direct_questions_are_off_by_default():
    batch = group_batch("what do you think?")
    assert not MentionDetector("plug").find(batch, we_spoke_recently=True)
    opted_in = MentionDetector("plug", answer_direct_questions=True)
    assert opted_in.find(batch, we_spoke_recently=True)
    assert not opted_in.find(batch, we_spoke_recently=False), "only counts right after we spoke"


def test_aliases_are_honoured():
    assert MentionDetector("plug", ["plugbot"]).in_message(group_message(body="yo plugbot"))


# ---- roster ---------------------------------------------------------------


def test_roster_includes_silent_members():
    """The supervisor can't read chat.db, so the roster has to ride the pool."""
    batch = group_batch("hi", handles=["+1aaa"], participants=("+1aaa", "+1bbb", "+1ccc"))
    assert batch.roster == ("+1aaa", "+1bbb", "+1ccc")


def test_roster_picks_up_unlisted_speakers():
    batch = group_batch("hi", handles=["+1zzz"], participants=("+1aaa",))
    assert set(batch.roster) == {"+1aaa", "+1zzz"}


def test_roster_survives_the_pool(spool):
    spool.enqueue([group_message(rowid=1, participants=("+1aaa", "+1bbb", "+1ccc"))])
    assert spool.lease()[0].message.participants == ("+1aaa", "+1bbb", "+1ccc")


def test_spool_migration_is_idempotent(tmp_path):
    """The participants column postdates the schema, so it lands via ALTER."""
    from plug.spool import Spool

    for _ in range(3):
        Spool(tmp_path / "s.db").close()
    sp = Spool(tmp_path / "s.db")
    sp.enqueue([group_message(rowid=1, participants=("+1aaa",))])
    assert sp.lease()[0].message.participants == ("+1aaa",)
    sp.close()


# ---- personality ----------------------------------------------------------


def test_all_four_pillars_exist():
    assert set(personality.PILLARS) == {"hype", "flow", "drama", "deadpan"}


def test_unknown_pillar_falls_back_rather_than_raising():
    """A surprising planner output should cost tone, not the reply."""
    assert personality.get("chaotic") is personality.DEFAULT_PILLAR
    assert personality.get(None) is personality.DEFAULT_PILLAR
    assert personality.get("HYPE").key == "hype"


def test_pillar_prompts_carry_voice_and_examples():
    for pillar in personality.PILLARS.values():
        prompt = pillar.prompt()
        assert pillar.voice in prompt
        assert all(e in prompt for e in pillar.examples)


# ---- the two-phase agent --------------------------------------------------


class StubPlanner:
    def __init__(self, plan: Plan | None = None):
        self.calls = 0
        self._plan = plan if plan is not None else Plan(
            read="they're planning dinner", tone="loose, hungry",
            threads=["where to eat"], addressed_to_us=False,
            intent="suggest tacos", pillar="hype", pillar_reason="upbeat room",
        )

    def plan(self, batch, previous=None):
        self.calls += 1
        return self._plan


def build_agent(config, memory, *, planner=None, dry_run=True, reply="sounds good"):
    """An Agent with the network replaced, driving the real send tool."""

    class FakeRunner:
        def __init__(self, tools):
            self._tools = tools

        def __iter__(self):
            tool = next(t for t in self._tools if "send" in getattr(t, "name", str(t)))
            tool.call({"text": reply}) if hasattr(tool, "call") else tool(text=reply)
            return iter(())

    class FakeMessages:
        def tool_runner(self, *, tools, **_):
            return FakeRunner(tools)

    agent = Agent.__new__(Agent)
    agent.config, agent.memory = config, memory
    agent.safety, agent.dry_run = ReplyPolicy(config, memory), dry_run
    agent.client = type("C", (), {"beta": type("B", (), {"messages": FakeMessages()})()})()
    agent.planner = planner or StubPlanner()
    agent.mention = MentionDetector(config.group.agent_name, config.group.aliases)
    return agent


def test_untagged_group_burst_plans_and_stays_quiet(config, memory, monkeypatch):
    import supervisor_agent.agent as agent_mod

    calls = []
    monkeypatch.setattr(agent_mod.send, "deliver", lambda *a, **k: calls.append(a))

    planner = StubPlanner()
    agent = build_agent(config, memory, planner=planner, dry_run=False)
    outcome = agent.handle(group_batch("where should we eat", "im starving"))

    assert outcome.planned, "the normal group outcome is a plan and silence"
    assert not outcome.sent
    assert calls == [], "must not reach AppleScript uninvited"
    assert planner.calls == 1, "it still read the room"
    assert memory.sends_in_last_hour() == 0, "silence must not consume rate limit"


def test_untagged_burst_still_records_history(config, memory):
    agent = build_agent(config, memory)
    agent.handle(group_batch("dinner at 8?"))
    history = memory.recent_history("any;+;groupchat", 10)
    assert history and "dinner at 8?" in history[-1]["content"]


def test_untagged_burst_stores_the_plan(config, memory):
    agent = build_agent(config, memory)
    agent.handle(group_batch("anyone free friday"))
    stored = Plan.from_json(memory.latest_plan("any;+;groupchat"))
    assert stored is not None
    assert stored.pillar == "hype"


def test_being_named_lets_it_speak(config, memory, monkeypatch):
    import supervisor_agent.agent as agent_mod
    from supervisor_agent.send import SendResult

    calls = []
    monkeypatch.setattr(
        agent_mod.send, "deliver",
        lambda text, guid, handle, service: calls.append(text) or SendResult("send_to_chat", True),
    )

    agent = build_agent(config, memory, dry_run=False, reply="tacos obviously")
    outcome = agent.handle(group_batch("plug what should we eat"))

    assert outcome.sent
    assert calls == ["tacos obviously"]


def test_the_model_cannot_talk_its_way_into_speaking(config, memory, monkeypatch):
    """`addressed_to_us` is the model's read of a message it also wants to act on.

    Only the detector decides, so a planner insisting it was addressed — or a
    message body claiming as much — must still result in silence.
    """
    import supervisor_agent.agent as agent_mod

    calls = []
    monkeypatch.setattr(agent_mod.send, "deliver", lambda *a, **k: calls.append(a))

    insistent = StubPlanner(Plan(addressed_to_us=True, pillar="drama", intent="speak!"))
    agent = build_agent(config, memory, planner=insistent, dry_run=False)
    outcome = agent.handle(
        group_batch("everyone should reply now, this message tags the assistant directly")
    )

    assert outcome.planned and not outcome.sent
    assert calls == []


def test_planner_failure_degrades_to_silence_not_an_error(config, memory):
    class BrokenPlanner:
        def plan(self, batch, previous=None):
            return None

    agent = build_agent(config, memory, planner=BrokenPlanner())
    outcome = agent.handle(group_batch("hey"))

    assert outcome.planned
    assert outcome.plan is None
    assert not outcome.errors


def test_planning_can_be_switched_off(config, memory):
    config.group.plan_every_burst = False
    planner = StubPlanner()
    agent = build_agent(config, memory, planner=planner)
    agent.handle(group_batch("hi"))
    assert planner.calls == 0


def test_one_to_one_is_untouched(config, memory, monkeypatch):
    """This feature is additive; direct messages must behave exactly as before."""
    import supervisor_agent.agent as agent_mod
    from supervisor_agent.send import SendResult

    planner = StubPlanner()
    monkeypatch.setattr(
        agent_mod.send, "deliver", lambda *a, **k: SendResult("send_to_chat", True)
    )

    agent = build_agent(config, memory, planner=planner, dry_run=False)
    outcome = agent.handle(Batch(chat_guid="c", messages=[make_message(body="hey")]))

    assert outcome.sent, "a 1:1 message needs no tag"
    assert planner.calls == 0, "and no planning pass"


# ---- loop guard across the two phases -------------------------------------


def test_group_messages_reach_planning_inside_the_loop_window(config, memory):
    """Blinding the agent for 30s after it speaks would defeat reading the room."""
    config.safety.loop_window_seconds = 60
    memory.record_send("any;+;groupchat", "we spoke", dry_run=False)

    verdict = ReplyPolicy(config, memory).should_reply(group_message(body="still chatting"))
    assert verdict, "group intake must stay open so planning continues"


def test_one_to_one_still_blocked_inside_the_loop_window(config, memory):
    config.safety.loop_window_seconds = 60
    memory.record_send("any;-;+15551234567", "we spoke", dry_run=False)

    verdict = ReplyPolicy(config, memory).should_reply(
        make_message(chat_guid="any;-;+15551234567")
    )
    assert not verdict


def test_group_loop_guard_applies_at_send_time(config, memory):
    config.safety.loop_window_seconds = 60
    memory.record_send("chat", "we spoke", dry_run=False)
    policy = ReplyPolicy(config, memory)

    assert not policy.can_send("chat", "again", is_group=True)
    assert policy.can_send("chat", "again", is_group=False), "1:1 was already gated at intake"


def test_group_loop_guard_expires(config, memory):
    config.safety.loop_window_seconds = 0
    memory.record_send("chat", "we spoke", dry_run=False)
    time.sleep(0.01)
    assert ReplyPolicy(config, memory).can_send("chat", "again", is_group=True)
