"""ASGI entrypoint for the supervisor agent server.

    uvicorn supervisor_agent.main:app --port 8002

The drain loop runs on a worker thread started by the lifespan. The HTTP surface
adds the operational controls that matter most when this thing is live: the kill
switch, the dead-letter queue, and pool depth.

Environment:
    PLUG_CONFIG   path to the config file (default ``plug.yml``)
    PLUG_DRY_RUN  set to 1/true/yes to run the full pipeline without sending
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from plug import events, safety as safety_mod
from plug.config import JOBS_DB, SPOOL_DB, Config, load_dotenv
from plug.safety import ReplyPolicy
from plug.service import BackgroundLoop
from plug.spool import Spool

from .agent import Agent
from .jobs import JobStore
from .memory import Memory
from .server import SupervisorServer
from .workers import WorkerPool


load_dotenv()

CONFIG_PATH = os.environ.get("PLUG_CONFIG", "plug.yml")
DRY_RUN = os.environ.get("PLUG_DRY_RUN", "").lower() in {"1", "true", "yes", "on"}
config = Config.load(CONFIG_PATH)


def _build() -> tuple[SupervisorServer, list[Any]]:
    """Construct the loop and its resources inside the worker thread."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Fail startup loudly rather than dead-lettering every message with an
        # auth error a few seconds later.
        raise RuntimeError("ANTHROPIC_API_KEY is not set; the supervisor cannot reply")

    spool = Spool()
    memory = Memory()
    jobs = JobStore()
    policy = ReplyPolicy(config, memory)
    agent = Agent(config, memory, policy, dry_run=DRY_RUN, jobs=jobs)
    server = SupervisorServer(
        config, spool, memory, policy, agent, echo=False, jobs=jobs
    )
    # The pool is given the loop's store for claiming and the class itself for
    # worker threads, which must open their own connection.
    server.workers = WorkerPool(
        config, jobs, open_store=JobStore, client=agent.client, notify=server.notify
    )
    return server, [spool, memory, jobs]



loop = BackgroundLoop("supervisor", _build)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop.start()
    try:
        yield
    finally:
        # Stops the loop, which hands any buffered work back to the pool rather
        # than holding it until the lease expires.
        loop.stop()


app = FastAPI(
    title="Plug supervisor agent",
    summary="Leases pooled messages, replies via Messages.app, acknowledges.",
    lifespan=lifespan,
)


def _server() -> SupervisorServer:
    if loop.loop is None:
        raise HTTPException(503, "supervisor loop is not running")
    return loop.loop  # type: ignore[return-value]


@app.get("/health")
def health() -> dict[str, Any]:
    state = loop.health()
    state["dry_run"] = DRY_RUN
    state["paused"] = safety_mod.is_paused()
    if not state["running"]:
        raise HTTPException(503, state)
    return state


@app.get("/status")
def status() -> dict[str, Any]:
    server = _server()
    # Fresh connections per request — the loop's belong to its own thread.
    with Spool() as spool, Memory() as memory, JobStore() as jobs:
        pool = asdict(spool.stats())
        sends = memory.sends_in_last_hour()
        job_stats = asdict(jobs.stats())
    return {
        "owner": server.owner,
        "dry_run": DRY_RUN,
        "paused": safety_mod.is_paused(),
        "stats": asdict(server.stats),
        "pool": pool,
        "jobs": job_stats,
        "workers": {
            "enabled": config.workers.enabled,
            "kinds": config.workers.kinds,
            "running": server.workers.running if server.workers else 0,
            "max_concurrent": config.workers.max_concurrent,
            # Whether the research step actually has web access. The token is
            # never echoed; only whether one is present.
            "web_access": bool(os.environ.get(config.workers.brightdata.token_env)),
        },
        "sends_last_hour": sends,
        "config": {
            "model": config.model,
            "debounce_seconds": config.supervisor.debounce_seconds,
            "lease_seconds": config.supervisor.lease_seconds,
            "per_chat_per_hour": config.safety.per_chat_per_hour,
            "global_per_hour": config.safety.global_per_hour,
            "only_handles": config.chats.only_handles,
        },
        "paths": {"spool": str(SPOOL_DB), "jobs": str(JOBS_DB)},
        "config_source": config.source,
    }



@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    server = _server()
    with Spool() as spool:
        pool = spool.stats()

    lines = [
        "# HELP plug_supervisor_up Whether the drain loop is alive.",
        "# TYPE plug_supervisor_up gauge",
        f"plug_supervisor_up {1 if loop.running else 0}",
        "# TYPE plug_supervisor_paused gauge",
        f"plug_supervisor_paused {1 if safety_mod.is_paused() else 0}",
    ]
    for key, value in asdict(server.stats).items():
        lines += [
            f"# TYPE plug_supervisor_{key}_total counter",
            f"plug_supervisor_{key}_total {value}",
        ]
    for key, value in asdict(pool).items():
        if value is None:
            continue
        lines += [f"# TYPE plug_pool_{key} gauge", f"plug_pool_{key} {value}"]

    with JobStore() as jobs:
        for key, value in asdict(jobs.stats()).items():
            lines += [f"# TYPE plug_jobs_{key} gauge", f"plug_jobs_{key} {value}"]
    return "\n".join(lines) + "\n"


@app.get("/plan")

def plan(chat: str | None = None, limit: int = 5) -> dict[str, Any]:
    """What the agent is thinking about a group chat, without having spoken.

    Chats are keyed by the same hash the event log uses, so this can be read
    alongside `plug tail -f` without exposing chat identifiers. Pass no `chat`
    to list every conversation being followed.
    """
    with Memory() as memory:
        guids = memory.chats_with_plans()

        if chat is None:
            return {
                "following": [
                    {
                        "chat": events.anon(guid),
                        "latest": json.loads(memory.latest_plan(guid) or "null"),
                    }
                    for guid in guids
                ]
            }

        target = next((g for g in guids if events.anon(g) == chat or g == chat), None)
        if target is None:
            raise HTTPException(404, f"no plans recorded for {chat}")

        return {
            "chat": events.anon(target),
            "plans": [
                {"ts": ts, "plan": json.loads(payload)}
                for ts, payload in memory.recent_plans(target, limit)
            ],
        }


@app.get("/jobs")
def jobs(chat: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Background lookups the agent promised, and what became of them.

    Keyed by the same hash as the event log and `/plan`, so a job can be traced
    from the plan that proposed it to the message that answered it. ``findings``
    is deliberately absent — it is scraped page content, sometimes long, and the
    interesting part is the state machine.
    """
    with JobStore() as store:
        if chat is None:
            rows = store.recent(limit=limit)
        else:
            guids = store.chats()
            target = next((g for g in guids if events.anon(g) == chat or g == chat), None)
            if target is None:
                raise HTTPException(404, f"no jobs recorded for {chat}")
            rows = store.recent(target, limit=limit)

        return {
            "jobs": [
                {
                    "job": job.job_key,
                    "chat": events.anon(job.chat_guid),
                    "kind": job.kind,
                    "state": job.state,
                    "attempts": job.attempts,
                    "age_seconds": round(job.age, 1),
                    "objective": job.objective,
                    "reply": job.reply,
                    "note": job.note,
                }
                for job in rows
            ]
        }


@app.post("/notify")
def notify() -> dict[str, Any]:

    """Wake the drain loop. Called by the watchdog the moment it pools messages.

    Deliberately trivial: no auth, no payload contract, no message data. It only
    shortens the wait before the next poll, so a spurious call costs one extra
    (empty) pass over the pool. Bind to localhost in any deployment where that
    matters.
    """
    server = _server()
    server.notify()
    return {"woken": True}


@app.post("/pause")
def pause() -> dict[str, bool]:
    """Kill switch. Blocks the next send attempt; the watchdog keeps pooling."""
    safety_mod.pause()
    events.emit("safety", "paused", source="http")
    return {"paused": True}


@app.post("/resume")
def resume() -> dict[str, bool]:
    safety_mod.resume()
    events.emit("safety", "resumed", source="http")
    return {"paused": False}


@app.get("/dead")
def dead() -> dict[str, Any]:
    """Items that failed repeatedly. Bodies are omitted; this is an ops view."""
    with Spool() as spool:
        items = spool.dead_letters()
    return {
        "count": len(items),
        "items": [
            {
                "id": item.id,
                "attempts": item.attempts,
                "chat": events.anon(item.message.chat_guid),
                "service": item.message.service,
                "chars": len(item.message.body),
            }
            for item in items
        ],
    }


@app.post("/dead/requeue")
def requeue_dead() -> dict[str, int]:
    with Spool() as spool:
        return {"requeued": spool.requeue_dead()}
