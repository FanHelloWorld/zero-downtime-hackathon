"""Structured event log — one JSON object per line, one line per stage boundary.

This is the seam the later n8n-style canvas plugs into: the ``stage`` values are
the nodes, and the file is the replay source. Chat and handle identifiers are
hashed so the log isn't a plaintext mirror of the message database.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from . import eventlog
from .config import EVENT_LOG, ensure_state_dir


STAGES = ("watchdog", "buffer", "planner", "agent", "worker", "safety", "send")


_lock = threading.Lock()


def anon(value: str | None) -> str:
    """Stable short hash for an identifier, so logs correlate without exposing it."""
    if not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def emit(stage: str, event: str, *, chat: str | None = None, echo: bool = False, **detail: Any) -> None:
    """Append one event. ``detail`` is free-form and must stay JSON-serializable.

    Written twice, to two things that are good at different jobs: the JSONL file
    you tail when something is wrong, and an indexed table a UI can query and
    stream from. The second write cannot fail loudly — see ``eventlog``.
    """
    now = datetime.now(timezone.utc)
    key = anon(chat)
    record = {
        "ts": now.isoformat(),
        "stage": stage,
        "event": event,
        "chat": key,
        "detail": detail,
    }
    line = json.dumps(record, default=str)
    ensure_state_dir()
    with _lock:
        with EVENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    eventlog.record(
        stage, event, chat=key, detail=detail, ts=now.timestamp(), iso=record["ts"]
    )

    if echo:
        print(line, file=sys.stderr, flush=True)
