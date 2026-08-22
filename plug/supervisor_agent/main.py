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

import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from plug import events, safety as safety_mod
from plug.config import SPOOL_DB, Config, load_dotenv
from plug.safety import ReplyPolicy
from plug.service import BackgroundLoop
from plug.spool import Spool

from .agent import Agent
from .memory import Memory
from .server import SupervisorServer

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
    policy = ReplyPolicy(config, memory)
    agent = Agent(config, memory, policy, dry_run=DRY_RUN)
    server = SupervisorServer(config, spool, memory, policy, agent, echo=False)
    return server, [spool, memory]


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
    with Spool() as spool, Memory() as memory:
        pool = asdict(spool.stats())
        sends = memory.sends_in_last_hour()
    return {
        "owner": server.owner,
        "dry_run": DRY_RUN,
        "paused": safety_mod.is_paused(),
        "stats": asdict(server.stats),
        "pool": pool,
        "sends_last_hour": sends,
        "config": {
            "model": config.model,
            "debounce_seconds": config.supervisor.debounce_seconds,
            "lease_seconds": config.supervisor.lease_seconds,
            "per_chat_per_hour": config.safety.per_chat_per_hour,
            "global_per_hour": config.safety.global_per_hour,
            "only_handles": config.chats.only_handles,
        },
        "paths": {"spool": str(SPOOL_DB)},
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
    return "\n".join(lines) + "\n"


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
