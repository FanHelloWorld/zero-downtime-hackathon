"""The console API: everything the UI reads, and the few things it can press.

The console is the third server and the only one with no loop. What is worth
pinning here is that it stays a reader — the controls it exposes are doors onto
switches that already existed — and that chat identifiers do not leak through it,
since this is the surface that gets screenshared.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from plug.config import Config
from plug.eventlog import EventStore, record
from plug.safety import resume
from plug.spool import Spool
from supervisor_agent.jobs import DELIVERED, JobStore
from supervisor_agent.memory import Memory
from supervisor_agent.planner import Plan

from .conftest import make_message


@pytest.fixture(autouse=True)
def _unpaused():
    resume()
    yield
    resume()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point every store the console touches at a throwaway state directory."""
    import console.main as cmain
    import console.server as cserver
    from plug import eventlog as eventlog_mod

    paths = {
        "spool": tmp_path / "spool.db",
        "memory": tmp_path / "supervisor.db",
        "jobs": tmp_path / "jobs.db",
        "events": tmp_path / "events.db",
    }
    monkeypatch.setattr(eventlog_mod, "EVENTS_DB", paths["events"])
    for module in (cserver, cmain):
        for name, factory in (
            ("Spool", lambda: Spool(paths["spool"])),
            ("Memory", lambda: Memory(paths["memory"])),
            ("JobStore", lambda: JobStore(paths["jobs"])),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, factory)
    return paths


@pytest.fixture()
def client(isolated):
    import console.main as cmain

    with TestClient(cmain.app) as c:
        yield c


def seed_job(paths, chat="chat-a", state="ready"):
    with JobStore(paths["jobs"]) as jobs:
        job = jobs.enqueue(chat, "food", "dinner for three", is_group=True, handle="+1aaa")
        jobs.claim("w")
        jobs.ready(job.id, "findings", "el farolito")
        if state == "delivered":
            jobs.settle(job.id, DELIVERED, "sent")
        return job


def seed_events(paths, n=3):
    return [
        record("watchdog", "spooled", chat="chat-a", detail={"n": i}, path=paths["events"])
        for i in range(n)
    ]


# ---- reading ---------------------------------------------------------------


def test_health_reports_the_mode(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "dry_run" in body and "paused" in body


def test_overview_separates_what_it_heard_from_what_it_said(client, isolated):
    record("watchdog", "spooled", chat="c", detail={}, path=isolated["events"])
    record("agent", "planned", chat="c", detail={}, path=isolated["events"])
    record("agent", "planned", chat="c", detail={}, path=isolated["events"])
    record("send", "delivered", chat="c", detail={}, path=isolated["events"])

    body = client.get("/api/overview").json()
    assert body["heard"] == 1
    assert body["stayed_quiet"] == 2, "the gap between these two is the whole point"
    assert body["spoke"] == 1
    assert len(body["histogram"]) == 72


def test_the_graph_carries_the_fixed_pipeline(client):
    body = client.get("/api/graph").json()
    ids = {n["id"] for n in body["nodes"]}
    assert {"chat", "watchdog", "spool", "supervisor", "planner", "agent", "messages"} <= ids
    edges = {e["id"] for e in body["edges"]}
    assert "watchdog->spool" in edges and "supervisor->agent" in edges


def test_a_delegated_job_becomes_its_own_node(client, isolated):
    """A worker node is not decoration — a thread really is spawned per job."""
    job = seed_job(isolated)
    body = client.get("/api/graph").json()

    node = next(n for n in body["nodes"] if n["id"] == f"worker:{job.job_key}")
    assert node["status"] == "running"
    assert node["kind"] == "worker · food"

    edges = {e["id"] for e in body["edges"]}
    assert f"agent->worker:{job.job_key}" in edges
    assert f"worker:{job.job_key}->brightdata" in edges


def test_artifacts_include_jobs_and_what_was_merely_considered(client, isolated):
    seed_job(isolated)
    with Memory(isolated["memory"]) as memory:
        memory.save_plan("chat-b", Plan(
            tone="hungry",
            action={"kind": "food", "warranted": True,
                    "objective": "somewhere for friday", "missing": ["budget"]},
        ).to_json())

    kinds = [a["kind"] for a in client.get("/api/artifacts").json()["artifacts"]]
    assert "ready" in kinds
    assert "proposal" in kinds, "a plan that named a worker is a proposal, not a job"


def test_a_plan_that_proposed_nothing_is_not_an_artifact(client, isolated):
    with Memory(isolated["memory"]) as memory:
        memory.save_plan("chat-b", Plan(
            action={"kind": "none", "warranted": False, "objective": "", "missing": []},
        ).to_json())
    assert client.get("/api/artifacts").json()["artifacts"] == []


def test_agents_lists_the_always_on_parts_plus_workers(client, isolated):
    seed_job(isolated)
    names = [a["name"] for a in client.get("/api/agents").json()["agents"]]
    assert "@planner" in names and "@agent" in names
    assert any(n.startswith("@food:") for n in names)


def test_log_returns_newest_first_with_a_cursor(client, isolated):
    seed_events(isolated, 3)
    body = client.get("/api/log?limit=2").json()
    assert [e["detail"]["n"] for e in body["log"]] == [2, 1]
    assert body["cursor"] >= 3


def test_dossier_404s_for_a_chat_it_has_never_seen(client):
    assert client.get("/api/dossier/nope").status_code == 404


# ---- privacy ---------------------------------------------------------------


def test_real_chat_identifiers_do_not_reach_the_ui(client, isolated):
    """This is the surface that gets screenshared."""
    seed_job(isolated, chat="iMessage;+;real-group-guid")
    for route in ("/api/graph", "/api/artifacts", "/api/agents"):
        assert "real-group-guid" not in client.get(route).text, route


def test_revealing_handles_is_opt_in(isolated, monkeypatch):
    import console.main as cmain
    from console.server import ConsoleData

    seed_job(isolated, chat="iMessage;+;real-group-guid")
    revealing = Config()
    revealing.console.reveal_handles = True
    monkeypatch.setattr(cmain, "data", ConsoleData(revealing))

    with TestClient(cmain.app) as c:
        assert "real-group-guid" in c.get("/api/graph").text


# ---- the stream ------------------------------------------------------------


def test_the_stream_sends_frames_from_a_cursor(client, isolated):
    seed_events(isolated, 3)
    with client.stream("GET", "/api/stream?cursor=0&limit=3&quiet_timeout=3") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = list(resp.iter_lines())

    payloads = [json.loads(f[5:]) for f in frames if f.startswith("data:")]

    assert [p["detail"]["n"] for p in payloads] == [0, 1, 2], "oldest first"
    assert any(f.startswith("id:") for f in frames), "ids are what make resume work"


def test_last_event_id_resumes_rather_than_replaying(client, isolated):
    ids = seed_events(isolated, 3)
    with client.stream(
        "GET", "/api/stream?limit=1&quiet_timeout=3", headers={"Last-Event-ID": str(ids[1])}
    ) as resp:
        frames = list(resp.iter_lines())

    payload = json.loads(next(f for f in frames if f.startswith("data:"))[5:])
    assert payload["detail"]["n"] == 2, "a slept laptop resumes, it does not start over"


def test_a_junk_last_event_id_falls_back_instead_of_failing(client, isolated):
    seed_events(isolated, 2)
    with client.stream(
        "GET", "/api/stream?limit=1&quiet_timeout=3", headers={"Last-Event-ID": "garbage"}
    ) as resp:
        assert resp.status_code == 200
        assert any(f.startswith("data:") for f in resp.iter_lines())


def test_a_bounded_stream_gives_up_when_nothing_happens(client, isolated):
    """Otherwise the only way out of the generator is a client disconnect."""
    with client.stream("GET", "/api/stream?cursor=999999&limit=5&quiet_timeout=0.5") as resp:
        assert [f for f in resp.iter_lines() if f.startswith("data:")] == []



# ---- controls --------------------------------------------------------------


def test_pause_and_resume_drive_the_existing_kill_switch(client):
    from plug.safety import is_paused

    assert client.post("/api/control/pause").json() == {"paused": True}
    assert is_paused(), "the console must use the same switch, not its own"
    assert client.post("/api/control/resume").json() == {"paused": False}
    assert not is_paused()


def test_cancelling_a_job_settles_it_without_sending(client, isolated):
    job = seed_job(isolated)
    assert client.post(f"/api/jobs/{job.job_key}/cancel").json()["state"] == "blocked"
    with JobStore(isolated["jobs"]) as jobs:
        assert jobs.deliverable() == [], "a cancelled lookup delivers nothing"


def test_cancelling_an_unknown_job_is_a_404(client):
    assert client.post("/api/jobs/j_nope/cancel").status_code == 404


def test_dispatch_needs_a_chat_and_an_objective(client):
    assert client.post("/api/dispatch", json={}).status_code == 400
    assert client.post("/api/dispatch", json={"chat": "x"}).status_code == 400


def test_dispatch_refuses_a_worker_kind_that_is_not_enabled(client):
    body = {"chat": "x", "objective": "y", "kind": "astrology"}
    assert client.post("/api/dispatch", json=body).status_code == 400


def test_dispatch_404s_for_a_chat_the_backend_has_never_seen(client):
    """The UI holds hashes, so it can never name a conversation that isn't real."""
    body = {"chat": "made-up-hash", "objective": "dinner"}
    assert client.post("/api/dispatch", json=body).status_code == 404


def test_dispatch_files_a_job_for_a_chat_nobody_tagged(client, isolated):
    from plug.events import anon

    guid = "iMessage;+;group"
    with Spool(isolated["spool"]) as spool:
        spool.enqueue([make_message(rowid=1, chat_guid=guid, handle="+1aaa")])
    with Memory(isolated["memory"]) as memory:
        memory.save_plan(guid, Plan().to_json())

    body = {"chat": anon(guid), "objective": "somewhere for friday"}
    result = client.post("/api/dispatch", json=body)
    assert result.status_code == 200, result.text

    with JobStore(isolated["jobs"]) as jobs:
        filed = jobs.recent()[0]
        assert filed.chat_guid == guid
        assert filed.objective == "somewhere for friday"
        assert filed.handle == "+1aaa", "the send strategies still need something to aim at"


def test_dispatch_will_not_stack_two_lookups_on_one_chat(client, isolated):
    from plug.events import anon

    guid = "iMessage;+;group"
    with Spool(isolated["spool"]) as spool:
        spool.enqueue([make_message(rowid=1, chat_guid=guid)])
    with Memory(isolated["memory"]) as memory:
        memory.save_plan(guid, Plan().to_json())

    body = {"chat": anon(guid), "objective": "dinner"}
    assert client.post("/api/dispatch", json=body).status_code == 200
    assert client.post("/api/dispatch", json=body).status_code == 409


def test_dispatch_can_be_switched_off(isolated, monkeypatch):
    import console.main as cmain

    locked = Config()
    locked.console.allow_dispatch = False
    monkeypatch.setattr(cmain, "config", locked)

    with TestClient(cmain.app) as c:
        body = {"chat": "x", "objective": "y"}
        assert c.post("/api/dispatch", json=body).status_code == 403


# ---- redaction --------------------------------------------------------------


def test_handles_written_into_free_text_are_redacted(client, isolated):
    """Anonymising the chat key is not enough — the model writes numbers itself."""
    with Memory(isolated["memory"]) as memory:
        memory.save_plan("chat-b", Plan(
            read="+13107365085 is asking where to eat",
            action={
                "kind": "food", "warranted": True,
                "objective": "somewhere near +1 510-754-9467",
                "missing": ["where +19258040698 lives"],
            },
        ).to_json())

    body = client.get("/api/artifacts").text
    for number in ("13107365085", "510-754-9467", "19258040698"):
        assert number not in body, number


def test_ordinary_numbers_survive_redaction(client, isolated):
    """A time or a price is not a phone number, and blanking it would be worse."""
    with Memory(isolated["memory"]) as memory:
        memory.save_plan("chat-b", Plan(
            read="a table at 7:30pm for 3 people",
            action={"kind": "food", "warranted": True,
                    "objective": "under 45 a head", "missing": []},
        ).to_json())

    artifact = client.get("/api/artifacts").json()["artifacts"][0]
    assert "7:30pm" in artifact["read"]
    assert "45" in artifact["title"]


def test_revealing_handles_turns_redaction_off(isolated, monkeypatch):
    import console.main as cmain
    from console.server import ConsoleData

    with Memory(isolated["memory"]) as memory:
        memory.save_plan("chat-b", Plan(
            read="+13107365085 asked",
            action={"kind": "food", "warranted": True, "objective": "x", "missing": []},
        ).to_json())

    revealing = Config()
    revealing.console.reveal_handles = True
    monkeypatch.setattr(cmain, "data", ConsoleData(revealing))
    with TestClient(cmain.app) as c:
        assert "13107365085" in c.get("/api/artifacts").text
