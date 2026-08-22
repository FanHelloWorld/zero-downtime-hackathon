"""The job store: promises the agent made and has not kept yet.

Same contract as the pool — claim once, survive a crash, settle terminally — but
for work the supervisor generates itself rather than work the watchdog handed it.
"""

from __future__ import annotations

import time

import pytest

from supervisor_agent.jobs import (
    BLOCKED,
    DELIVERED,
    QUEUED,
    READY,
    RUNNING,
    JobStore,
)


@pytest.fixture()
def jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    yield store
    store.close()


def file_one(jobs, chat="chat-a", objective="find tacos"):
    return jobs.enqueue(chat, "food", objective, is_group=True, handle="+1aaa")


# ---- the state machine ----------------------------------------------------


def test_a_new_job_is_queued_and_has_a_public_id(jobs):
    job = file_one(jobs)
    assert job.state == QUEUED
    assert job.job_key.startswith("j_")
    assert job.attempts == 0


def test_claiming_marks_it_running_and_counts_the_attempt(jobs):
    file_one(jobs)
    claimed = jobs.claim("worker-1")
    assert [j.state for j in claimed] == [RUNNING]
    assert claimed[0].attempts == 1


def test_a_claimed_job_is_not_handed_out_twice(jobs):
    file_one(jobs)
    assert len(jobs.claim("worker-1", limit=5)) == 1
    assert jobs.claim("worker-2", limit=5) == [], "two workers must not run one job"


def test_a_lapsed_lease_is_retaken(jobs):
    file_one(jobs)
    jobs.claim("worker-1", lease_seconds=-1)
    assert len(jobs.claim("worker-2")) == 1, "a dead worker must not strand the promise"


def test_ready_carries_the_findings_and_the_message(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    jobs.ready(job.id, "three taquerias", "el farolito, it's the one")
    ready = jobs.deliverable()
    assert [j.job_key for j in ready] == [job.job_key]
    assert ready[0].reply == "el farolito, it's the one"


def test_delivering_settles_it(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    jobs.ready(job.id, "f", "r")
    jobs.settle(job.id, DELIVERED, "send_to_chat")
    assert jobs.deliverable() == []
    assert jobs.stats().delivered == 1


def test_failure_retries_until_the_budget_runs_out(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    assert jobs.fail(job.id, "boom", max_attempts=2) == QUEUED
    jobs.claim("w")
    assert jobs.fail(job.id, "boom", max_attempts=2) == "failed"


def test_a_note_records_why_without_changing_what(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    jobs.ready(job.id, "", "no luck")
    jobs.note(job.id, "expired")
    ready = jobs.deliverable()
    assert ready[0].state == READY, "still deliverable — it's an apology, not a success"
    assert ready[0].note == "expired"


def test_stale_reports_but_does_not_settle(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    assert jobs.stale(timeout_seconds=1000) == [], "not old enough yet"
    stale = jobs.stale(timeout_seconds=-1)
    assert [j.job_key for j in stale] == [job.job_key]
    assert jobs.stats().running == 1, "settling is the caller's decision"


def test_shutdown_hands_running_jobs_back(jobs):
    file_one(jobs)
    jobs.claim("worker-1")
    assert jobs.release("worker-1") == 1
    assert jobs.stats().queued == 1


def test_release_only_touches_this_owner(jobs):
    file_one(jobs)
    jobs.claim("worker-1")
    assert jobs.release("someone-else") == 0


# ---- quotas ---------------------------------------------------------------


def test_active_for_chat_counts_everything_not_yet_settled(jobs):
    job = file_one(jobs)
    assert jobs.active_for_chat("chat-a") == 1
    jobs.claim("w")
    assert jobs.active_for_chat("chat-a") == 1
    jobs.ready(job.id, "f", "r")
    assert jobs.active_for_chat("chat-a") == 1, "a written but unsent answer still counts"
    jobs.settle(job.id, DELIVERED)
    assert jobs.active_for_chat("chat-a") == 0


def test_quota_counters_are_per_chat(jobs):
    file_one(jobs, chat="chat-a")
    file_one(jobs, chat="chat-b")
    assert jobs.count_since("chat-a", 3600) == 1
    assert jobs.seconds_since_last("chat-a") < 5
    assert jobs.seconds_since_last("never-heard-of-it") is None


# ---- housekeeping ---------------------------------------------------------


def test_purge_spares_anything_still_in_flight(jobs):
    live = file_one(jobs, chat="chat-live")
    done = file_one(jobs, chat="chat-done")
    jobs.settle(done.id, BLOCKED, "paused")
    assert jobs.purge(older_than_seconds=-1) == 1
    assert [j.job_key for j in jobs.recent()] == [live.job_key]


def test_reopening_the_store_is_safe(tmp_path):
    path = tmp_path / "jobs.db"
    first = JobStore(path)
    file_one(first)
    first.close()
    second = JobStore(path)
    assert len(second.recent()) == 1, "re-init must not wipe or fail"
    second.close()


def test_recent_is_newest_first_and_scopeable(jobs):
    file_one(jobs, chat="chat-a", objective="first")
    time.sleep(0.01)
    file_one(jobs, chat="chat-b", objective="second")
    assert [j.objective for j in jobs.recent()] == ["second", "first"]
    assert [j.objective for j in jobs.recent("chat-a")] == ["first"]


def test_time_queued_does_not_count_against_the_timeout(jobs):
    """A job that waited behind a busy pool has not used any of its own time."""
    job = file_one(jobs)
    jobs._conn.execute(
        "UPDATE jobs SET created_at = created_at - 10000 WHERE id = ?", (job.id,)
    )
    jobs._conn.commit()

    jobs.claim("w")
    assert jobs.stale(timeout_seconds=60) == [], "it only just started running"
    assert jobs.recent()[0].running_for < 5


def test_a_retry_restarts_the_clock(jobs):
    job = file_one(jobs)
    jobs.claim("w")
    jobs.fail(job.id, "boom", max_attempts=5)
    jobs.claim("w")
    assert jobs.stale(timeout_seconds=60) == []
