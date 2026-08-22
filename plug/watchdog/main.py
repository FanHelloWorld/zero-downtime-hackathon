"""ASGI entrypoint for the watchdog server.

    uvicorn watchdog.main:app --port 8001

The poll loop runs on a worker thread started by the lifespan; the HTTP surface
is for health checks, status, and operating the loop. This server still needs no
API key and never sends anything.

Configuration comes from ``PLUG_CONFIG`` (default ``plug.yml``), since uvicorn
gives us no place to pass CLI flags.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from plug.config import CHAT_DB, SPOOL_DB, Config, load_dotenv
from plug.service import BackgroundLoop
from plug.spool import Spool

from .db import ChatDB
from .server import WatchdogServer
from .state import WatchdogState

load_dotenv()

CONFIG_PATH = os.environ.get("PLUG_CONFIG", "plug.yml")
config = Config.load(CONFIG_PATH)


def _build() -> tuple[WatchdogServer, list[Any]]:
    """Construct the loop and its resources *inside* the worker thread.

    SQLite connections are bound to the thread that opens them, so this must not
    run at import time or in the lifespan's own thread.
    """
    db = ChatDB()
    state = WatchdogState()
    spool = Spool()
    state.seed_cursor(db.max_rowid())
    server = WatchdogServer(config, db, state, spool, echo=False)
    return server, [db, state, spool]


loop = BackgroundLoop("watchdog", _build)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop.start()
    try:
        yield
    finally:
        loop.stop()


app = FastAPI(
    title="Plug watchdog",
    summary="Polls the Messages database and pools new messages to disk.",
    lifespan=lifespan,
)


def _server() -> WatchdogServer:
    if loop.loop is None:
        raise HTTPException(503, "watchdog loop is not running")
    return loop.loop  # type: ignore[return-value]


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness. Returns 503 once the loop has died, so a supervisor restarts us."""
    state = loop.health()
    if not state["running"]:
        raise HTTPException(503, state)
    return state


@app.get("/status")
def status() -> dict[str, Any]:
    server = _server()
    # Fresh connections per request. The loop's own ChatDB/WatchdogState/Spool
    # belong to the worker thread, and SQLite refuses cross-thread use — reading
    # server.state here raises ProgrammingError. Only plain values, like the
    # stats dataclass, are safe to read across the boundary.
    with Spool() as spool, WatchdogState() as state:
        pool = asdict(spool.stats())
        cursor = state.get_cursor()
    return {
        "cursor": cursor,
        "stats": asdict(server.stats),
        "pool": pool,
        "config": {
            "poll_interval_seconds": config.watchdog.poll_interval_seconds,
            "read_limit": config.watchdog.read_limit,
            "services": config.chats.services,
            "include_groups": config.chats.include_groups,
        },
        "paths": {"chat_db": str(CHAT_DB), "spool": str(SPOOL_DB)},
        "config_source": config.source,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus text exposition, so this is scrapeable without extra glue."""
    server = _server()
    with Spool() as spool:
        pool = spool.stats()

    lines = [
        "# HELP plug_watchdog_up Whether the poll loop is alive.",
        "# TYPE plug_watchdog_up gauge",
        f"plug_watchdog_up {1 if loop.running else 0}",
    ]
    for key, value in asdict(server.stats).items():
        lines += [
            f"# TYPE plug_watchdog_{key}_total counter",
            f"plug_watchdog_{key}_total {value}",
        ]
    for key, value in asdict(pool).items():
        if value is None:
            continue
        lines += [
            f"# TYPE plug_pool_{key} gauge",
            f"plug_pool_{key} {value}",
        ]
    return "\n".join(lines) + "\n"


@app.post("/tick")
def tick() -> dict[str, int]:
    """Force an immediate poll instead of waiting out the interval.

    Handy for demos and integration tests. It runs on the request thread, so it
    opens its own resources rather than borrowing the loop's.
    """
    db = ChatDB()
    state = WatchdogState()
    spool = Spool()
    try:
        server = WatchdogServer(config, db, state, spool, echo=False)
        return {"spooled": server.tick()}
    finally:
        db.close()
        state.close()
        spool.close()
