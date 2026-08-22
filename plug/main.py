"""Both servers in one uvicorn process, for local runs and single-container deploys.

    uvicorn main:app --port 8000

    /watchdog/...    the watchdog server's routes
    /supervisor/...  the supervisor agent's routes
    /health          both loops at once

This is a convenience, not the recommended production shape. Running them
together gives up the main reason they were separated: one process means one
crash, one restart, one set of permissions, and the component holding Full Disk
Access shares an address space with the one holding your API key. Prefer two
uvicorn processes:

    uvicorn watchdog.main:app --port 8001
    uvicorn supervisor_agent.main:app --port 8002
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from supervisor_agent.main import app as supervisor_app, loop as supervisor_loop
from watchdog.main import app as watchdog_app, loop as watchdog_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mounted sub-apps do not get their lifespan run, so start both loops here.
    # BackgroundLoop.start() is idempotent, so this stays correct even if a
    # future Starlette does propagate lifespan to mounts.
    watchdog_loop.start()
    try:
        supervisor_loop.start()
    except Exception:
        # Don't leave a half-started system: if the agent can't come up, the
        # watchdog shouldn't quietly pool messages nobody will answer.
        watchdog_loop.stop()
        raise

    try:
        yield
    finally:
        # Stop the consumer first so it can hand buffered work back to the pool
        # before the producer stops adding to it.
        supervisor_loop.stop()
        watchdog_loop.stop()


app = FastAPI(
    title="Plug",
    summary="iMessage watchdog and supervisor agent, sharing a disk pool.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    state = {
        "watchdog": watchdog_loop.health(),
        "supervisor": supervisor_loop.health(),
    }
    if not all(part["running"] for part in state.values()):
        raise HTTPException(503, state)
    return state


app.mount("/watchdog", watchdog_app)
app.mount("/supervisor", supervisor_app)
