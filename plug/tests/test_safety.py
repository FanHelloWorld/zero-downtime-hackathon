"""Each guard must block on its own.

The split matters: ``IngestFilter`` runs in the watchdog and must stay stateless,
while ``ReplyPolicy`` runs in the supervisor and may consult the send log.
"""

from __future__ import annotations

import time

import pytest

from plug.safety import IngestFilter, ReplyPolicy, pause, resume

from .conftest import make_message


@pytest.fixture(autouse=True)
def _unpaused():
    resume()
    yield
    resume()


# ---- watchdog side: IngestFilter -----------------------------------------


def test_ingest_allows_an_ordinary_message(config):
    assert IngestFilter(config).should_ingest(make_message())


def test_ingest_rejects_empty_bodies(config):
    assert not IngestFilter(config).should_ingest(make_message(body="   "))


def test_ingest_respects_group_toggle(config, group_message):
    assert IngestFilter(config).should_ingest(group_message)
    config.chats.include_groups = False
    assert not IngestFilter(config).should_ingest(group_message)


def test_ingest_respects_1to1_toggle(config):
    config.chats.include_1to1 = False
    assert not IngestFilter(config).should_ingest(make_message())


def test_ingest_service_filter(config):
    config.chats.services = ["iMessage"]
    guard = IngestFilter(config)
    assert guard.should_ingest(make_message(service="iMessage"))
    assert not guard.should_ingest(make_message(service="SMS"))


def test_ingest_filter_needs_no_state(config):
    """It must be constructible without a database — the watchdog has none."""
    IngestFilter(config).should_ingest(make_message())


# ---- supervisor side: ReplyPolicy ----------------------------------------


def test_policy_allows_an_ordinary_message(config, memory):
    policy = ReplyPolicy(config, memory)
    assert policy.should_reply(make_message())
    assert policy.can_send("chat", "sure, on my way")


def test_kill_switch_blocks_sending(config, memory):
    policy = ReplyPolicy(config, memory)
    pause()
    try:
        verdict = policy.can_send("chat", "hello")
        assert not verdict
        assert "paused" in verdict.reason
    finally:
        resume()
    assert policy.can_send("chat", "hello")


def test_kill_switch_is_not_cached(config, memory):
    """PAUSE must take effect immediately, not at next startup."""
    policy = ReplyPolicy(config, memory)
    assert policy.can_send("chat", "hello")
    pause()
    assert not policy.can_send("chat", "hello")


def test_per_chat_rate_limit(config, memory):
    config.safety.per_chat_per_hour = 2
    policy = ReplyPolicy(config, memory)
    for _ in range(2):
        memory.record_send("chat-a", "hi", dry_run=False)
    assert not policy.can_send("chat-a", "hi")
    assert policy.can_send("chat-b", "hi"), "a different chat is unaffected"


def test_global_rate_limit(config, memory):
    config.safety.per_chat_per_hour = 100
    config.safety.global_per_hour = 3
    policy = ReplyPolicy(config, memory)
    for i in range(3):
        memory.record_send(f"chat-{i}", "hi", dry_run=False)
    assert not policy.can_send("chat-new", "hi")


def test_dry_run_sends_do_not_consume_rate_limit(config, memory):
    config.safety.per_chat_per_hour = 1
    policy = ReplyPolicy(config, memory)
    memory.record_send("chat-a", "hi", dry_run=True)
    assert policy.can_send("chat-a", "hi")


def test_loop_guard_suppresses_immediate_reply(config, memory):
    config.safety.loop_window_seconds = 60
    policy = ReplyPolicy(config, memory)
    msg = make_message()
    memory.record_send(msg.chat_guid, "our reply", dry_run=False)
    verdict = policy.should_reply(msg)
    assert not verdict
    assert "loop-guard" in verdict.reason


def test_loop_guard_expires(config, memory):
    config.safety.loop_window_seconds = 0
    policy = ReplyPolicy(config, memory)
    msg = make_message()
    memory.record_send(msg.chat_guid, "our reply", dry_run=False)
    time.sleep(0.01)
    assert policy.should_reply(msg)


@pytest.mark.parametrize(
    "body",
    [
        "Your verification code is 481920",
        "912833 is your one-time passcode. Do not share.",
        "Use code 55123 to verify your login",
    ],
)
def test_deny_patterns_block_otp_traffic(config, memory, body):
    assert not ReplyPolicy(config, memory).should_reply(make_message(body=body))


def test_short_code_senders_are_ignored(config, memory):
    policy = ReplyPolicy(config, memory)
    assert not policy.should_reply(make_message(handle="262966"))
    assert policy.should_reply(make_message(handle="+15551234567"))


def test_never_reply_to_list(config, memory):
    config.safety.never_reply_to = ["+15551234567"]
    assert not ReplyPolicy(config, memory).should_reply(make_message(handle="+15551234567"))


def test_only_handles_scopes_everything_else_out(config, memory):
    config.chats.only_handles = ["+15559999999"]
    policy = ReplyPolicy(config, memory)
    assert not policy.should_reply(make_message(handle="+15551234567"))
    assert policy.should_reply(make_message(handle="+15559999999"))


def test_reply_length_cap(config, memory):
    config.safety.max_reply_chars = 10
    assert not ReplyPolicy(config, memory).can_send("chat", "x" * 11)


def test_empty_reply_rejected(config, memory):
    assert not ReplyPolicy(config, memory).can_send("chat", "   ")


def test_agent_cannot_relay_an_otp_outward(config, memory):
    """Deny patterns apply to outbound text too, not just inbound."""
    assert not ReplyPolicy(config, memory).can_send("chat", "sure, the code is 481920 for verification")
