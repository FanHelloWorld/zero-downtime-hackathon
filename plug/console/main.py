"""ASGI entrypoint for the console.

    uvicorn console.main:app --port 8003

Or, more usually, mounted at /console by ``main.py`` at the repo root so one
uvicorn serves the watchdog, the supervisor, this, and the UI together.

Unlike the other two apps there is no background loop here: nothing to poll,
nothing to drive. Every request opens its own SQLite connections, reads, and
closes them — which is also what makes it safe to run alongside two servers that
are writing to the same files.

Environment:
    PLUG_CONFIG   path to the config file (default ``plug.yml``)
    PLUG_DRY_RUN  fallback only. The banner asks the supervisor, which is the
                  process that actually holds the send path.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from plug import safety as safety_mod
from plug.config import Config, load_dotenv
from plug.eventlog import EventStore
from plug.spool import Spool
from supervisor_agent.jobs import BLOCKED, JobStore

from .server import ConsoleData

load_dotenv()

CONFIG_PATH = os.environ.get("PLUG_CONFIG", "plug.yml")
# This process's own idea of dry run. Only ever a fallback: the flag that decides
# whether AppleScript runs is read by the supervisor, in the supervisor's
# environment, and the two are started from different shells often enough that
# trusting this one silently mislabels a muted system as live.
LOCAL_DRY_RUN = os.environ.get("PLUG_DRY_RUN", "").lower() in {"1", "true", "yes", "on"}
config = Config.load(CONFIG_PATH)
data = ConsoleData(config)

# Cache the supervisor's answer briefly. Every read endpoint wants it and the UI
# polls; one probe per window is plenty, and a stale-by-a-second banner is
# indistinguishable from a live one.
SEND_STATE_TTL = 2.0
_send_state_cache: tuple[float, dict[str, Any]] = (0.0, {})


def _probe_supervisor() -> dict[str, Any]:
    """Ask the supervisor whether sending is live. Never raises."""
    url = config.console.supervisor_health_url
    if not url:
        return {"dry_run": LOCAL_DRY_RUN, "source": "local-env", "verified": False}
    try:
        with urllib.request.urlopen(
            url, timeout=config.console.supervisor_health_timeout_seconds
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "dry_run": bool(payload.get("dry_run")),
            "source": "supervisor",
            "verified": True,
            "supervisor_running": bool(payload.get("running")),
        }
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        # Unreachable is not the same as live. Say so rather than guessing: a
        # supervisor we cannot see may be muted, may be down, may be sending.
        return {
            "dry_run": LOCAL_DRY_RUN,
            "source": "local-env",
            "verified": False,
            "supervisor_running": False,
            "error": repr(exc),
        }


async def send_state() -> dict[str, Any]:
    global _send_state_cache
    fetched_at, cached = _send_state_cache
    now = time.monotonic()
    if cached and (now - fetched_at) < SEND_STATE_TTL:
        return cached
    state = await asyncio.to_thread(_probe_supervisor)
    _send_state_cache = (now, state)
    return state

# Where a built frontend lands. Absent during development, when Vite serves it.
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

# How long a stream sits silent before sending a comment frame. Long enough to
# be cheap, short enough that an idle proxy does not decide the socket is dead.
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.25

app = FastAPI(
    title="Plug console",
    summary="Read-only view of the pipeline, plus the controls that already existed.",
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    state = await send_state()
    return {
        "ok": True,
        "dry_run": state["dry_run"],
        "dry_run_source": state["source"],
        "dry_run_verified": state["verified"],
        "supervisor_running": state.get("supervisor_running"),
        "paused": safety_mod.is_paused(),
    }


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    state = await send_state()
    found = await asyncio.to_thread(
        data.overview, dry_run=state["dry_run"], paused=safety_mod.is_paused()
    )
    found["dry_run_source"] = state["source"]
    found["dry_run_verified"] = state["verified"]
    found["supervisor_running"] = state.get("supervisor_running")
    return found


@app.get("/api/graph")
async def graph() -> dict[str, Any]:
    return await asyncio.to_thread(data.graph)


@app.get("/api/artifacts")
async def artifacts(limit: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(data.artifacts, limit)


@app.get("/api/agents")
async def agents() -> dict[str, Any]:
    return await asyncio.to_thread(data.agents)


@app.get("/api/log")
async def log(limit: int = 100, stage: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(data.log, limit, stage)


@app.get("/api/dossier/{chat_key}")
async def dossier(chat_key: str) -> dict[str, Any]:
    found = await asyncio.to_thread(data.dossier, chat_key)
    if found is None:
        raise HTTPException(404, f"nothing known about {chat_key}")
    return found


# ---- the stream ------------------------------------------------------------


def _read_since(cursor: int, limit: int = 200) -> list[dict[str, Any]]:
    """One poll, on a worker thread with its own connection."""
    with EventStore() as store:
        return store.since(cursor, limit)


def _starting_cursor(backfill: int) -> int:
    with EventStore() as store:
        return max(0, store.max_id() - backfill)


@app.get("/api/stream")
async def stream(
    request: Request,
    cursor: int | None = None,
    backfill: int = 50,
    limit: int = 0,
    quiet_timeout: float = 10.0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-sent events, cursored by the event table's autoincrement id.

    SSE rather than a WebSocket for one reason that matters here: the browser
    reconnects on its own and replays ``Last-Event-ID``, so a laptop that sleeps
    mid-run resumes exactly where it stopped instead of losing the pipeline it
    was showing. Nothing needs to flow the other way.

    ``limit`` bounds the stream: after that many events it closes cleanly, and it
    gives up after ``quiet_timeout`` seconds of nothing. The live UI leaves it at
    0 and stays connected; a caller that wants "everything I missed, then hang
    up" — a script, a test, a client that would rather poll — sets it. Without a
    bounded mode the only way out of this generator is a disconnect.
    """

    if last_event_id is not None:
        try:
            start = int(last_event_id)
        except ValueError:
            start = await asyncio.to_thread(_starting_cursor, backfill)
    elif cursor is not None:
        start = cursor
    else:
        # A fresh page gets a short tail so the canvas is not blank while it
        # waits for something to happen.
        start = await asyncio.to_thread(_starting_cursor, backfill)

    async def frames():
        position = start
        quiet_for = 0.0
        emitted = 0
        while True:
            if await request.is_disconnected():
                return
            if limit and quiet_for >= quiet_timeout:
                return

            try:
                rows = await asyncio.to_thread(_read_since, position)
            except Exception as exc:  # a stream must not 500 halfway through
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                await asyncio.sleep(1.0)
                continue

            if rows:
                quiet_for = 0.0
                for row in rows:
                    position = row["id"]
                    yield f"id: {row['id']}\nevent: pipeline\ndata: {json.dumps(row)}\n\n"
                    emitted += 1
                    if limit and emitted >= limit:
                        return
            else:

                quiet_for += POLL_SECONDS
                if quiet_for >= HEARTBEAT_SECONDS:
                    quiet_for = 0.0
                    yield ": keepalive\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx and friends buffer by default, which turns a live stream
            # into a batch delivered at the end.
            "X-Accel-Buffering": "no",
        },
    )


# ---- controls --------------------------------------------------------------
#
# Everything here already existed somewhere else. The console is a second door
# onto the same switches, not a new set of them.


@app.post("/api/control/pause")
def pause() -> dict[str, bool]:
    safety_mod.pause()
    return {"paused": True}


@app.post("/api/control/resume")
def resume() -> dict[str, bool]:
    safety_mod.resume()
    return {"paused": False}


@app.post("/api/jobs/{job_key}/cancel")
async def cancel(job_key: str) -> dict[str, Any]:
    """Stop waiting on a lookup. Terminal, and it never sends anything."""

    def _cancel() -> dict[str, Any] | None:
        with JobStore() as jobs:
            found = next((j for j in jobs.recent(limit=200) if j.job_key == job_key), None)
            if found is None:
                return None
            jobs.settle(found.id, BLOCKED, "cancelled from the console")
            return {"job": job_key, "state": BLOCKED}

    result = await asyncio.to_thread(_cancel)
    if result is None:
        raise HTTPException(404, f"no job {job_key}")
    return result


@app.post("/api/dispatch")
async def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Start a lookup for a chat nobody tagged.

    The one place the console can loosen the silence rule that ``plug/mention``
    enforces, and it is deliberate: a person pressing a button is a stronger
    statement of intent than their name appearing in a message. Everything after
    this point is unchanged — the worker runs normally and its follow-up still
    has to pass ``ReplyPolicy.can_send``, the kill switch, and dry run.
    """
    if not config.console.allow_dispatch:
        raise HTTPException(403, "console dispatch is disabled in config")

    chat_key = str(payload.get("chat") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    kind = str(payload.get("kind") or "food").strip()
    if not chat_key or not objective:
        raise HTTPException(400, "chat and objective are required")
    if kind not in config.workers.kinds:
        raise HTTPException(400, f"unknown or disabled worker kind {kind!r}")

    def _dispatch() -> dict[str, Any] | None:
        guid = data.resolve_chat(chat_key)
        if guid is None:
            return None
        with Spool() as spool, JobStore() as jobs:
            if jobs.active_for_chat(guid):
                return {"error": "that chat already has a lookup running"}
            last = spool.latest_for_chat(guid)
            job = jobs.enqueue(
                guid, kind, objective,
                context=str(payload.get("context") or ""),
                pillar=str(payload.get("pillar") or config.default_pillar),
                is_group=bool(last.message.is_group) if last else False,
                handle=last.message.handle if last else None,
                service=last.message.service if last else "iMessage",
            )
            return {"job": job.job_key, "state": job.state, "kind": kind}

    result = await asyncio.to_thread(_dispatch)
    if result is None:
        raise HTTPException(404, f"no chat matching {chat_key}")
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


# ---- the UI ----------------------------------------------------------------
#
# Mounted last so every /api route above wins the match.

if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
else:
    @app.get("/")
    def no_ui() -> dict[str, str]:
        return {
            "console": "API is up; no built UI found",
            "expected": str(WEB_DIST),
            "hint": "cd web && npm install && npm run build — or use the Vite dev server",
        }
