"""Shaping stored state into what the UI draws.

Pure reads. Every method opens its own connections, does its queries, and closes
them — the console's HTTP handlers run on the event loop and its data functions
run in worker threads, so nothing here may hold a connection across a call.

The pipeline graph is the interesting part. Its skeleton is fixed, because the
pipeline is fixed: chat.db → watchdog → pool → supervisor → agent → Messages.
What varies is the workers, and those are genuinely dynamic — one node per job
the agent delegated, appearing when it does. That is not a visual flourish; a
thread really is spawned per job.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any


from plug import events as events_mod
from plug.config import Config
from plug.eventlog import EventStore
from plug.spool import Spool
from supervisor_agent.dossier import Dossier
from supervisor_agent.jobs import READY, RUNNING, JobStore
from supervisor_agent.memory import Memory
from supervisor_agent.planner import Plan

# The fixed skeleton. `kind` drives which shape the UI draws; `id` is what the
# event stream lights up.
# `status` is the node's resting state — what it returns to once its glow decays.
# "always" is for the parts that are genuinely always running; everything else
# sits idle until an event wakes it.
PIPELINE_NODES: list[dict[str, Any]] = [
    {"id": "chat", "kind": "trigger", "name": "Messages", "status": "idle",
     "desc": "Your iMessage database. Read, never written."},
    {"id": "watchdog", "kind": "agent · always on", "name": "Watchdog", "status": "always",
     "desc": "Polls chat.db every 3s and pools what is new.",
     "tools": ["chat.db (ro)", "decode"]},
    {"id": "spool", "kind": "queue", "name": "Pool", "status": "idle",
     "desc": "Leased SQLite queue. The only thing the two servers share."},
    {"id": "supervisor", "kind": "agent · always on", "name": "Supervisor", "status": "always",
     "desc": "Leases, debounces a burst into one turn, delivers.",
     "tools": ["lease", "safety", "deliver"]},
    {"id": "planner", "kind": "reads the room", "name": "Planner", "status": "always",
     "desc": "Runs on every group burst, whether or not anyone gets a reply.",
     "tools": ["tone", "facts", "proposal"]},
    {"id": "agent", "kind": "decides", "name": "Reply agent", "status": "always",
     "desc": "Speaks only when named. Sends, skips, or delegates.",
     "tools": ["send_reply", "delegate", "skip"]},
    {"id": "brightdata", "kind": "external · mcp", "name": "Bright Data", "status": "idle",
     "desc": "Live web research, reached through Anthropic's MCP connector.",
     "tools": ["search_engine", "scrape_as_markdown"]},
    {"id": "messages", "kind": "output", "name": "Sent", "status": "idle",
     "desc": "AppleScript into Messages.app. The only way out."},
]


PIPELINE_EDGES: list[dict[str, str]] = [
    {"id": "chat->watchdog", "source": "chat", "target": "watchdog"},
    {"id": "watchdog->spool", "source": "watchdog", "target": "spool"},
    {"id": "spool->supervisor", "source": "spool", "target": "supervisor"},
    {"id": "supervisor->planner", "source": "supervisor", "target": "planner"},
    {"id": "supervisor->agent", "source": "supervisor", "target": "agent"},
    {"id": "agent->messages", "source": "agent", "target": "messages"},
    {"id": "supervisor->messages", "source": "supervisor", "target": "messages"},
]

# Job states that still deserve a node on the canvas.
LIVE_JOB_STATES = (RUNNING, READY, "queued")

# Phone numbers and email addresses appearing inside free text — a planner's read
# of the room, an objective, a note. Anonymising the chat key is not enough: the
# model writes handles into its own prose, and that prose renders on a screen
# someone is likely sharing.
_CONTACT = re.compile(r"\+?\d[\d\s().\-]{5,}\d|\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_MIN_DIGITS = 7


def _redact(match: re.Match[str]) -> str:
    text = match.group(0)
    if "@" in text:
        return "···"
    # Ordinary numbers stay: times, prices, counts. Only strings with enough
    # digits to actually be a phone number are worth hiding.
    return "···" if sum(c.isdigit() for c in text) >= _MIN_DIGITS else text



@dataclass(frozen=True, slots=True)
class ConsoleData:
    """Read-only views over the state directory."""

    config: Config

    # ---- identity ---------------------------------------------------------

    def _label(self, chat_guid: str, display: str | None = None) -> str:
        """What to call a chat on screen.

        Anonymised by default: this thing gets screenshared, and the event log
        already decided that chat identifiers are not for looking at.
        """
        if self.config.console.reveal_handles:
            return display or chat_guid
        return events_mod.anon(chat_guid)

    def _text(self, value: str | None) -> str:
        """Free text on its way to the screen, with contact details removed."""
        text = str(value or "")
        if self.config.console.reveal_handles:
            return text
        return _CONTACT.sub(_redact, text)


    # ---- overview ---------------------------------------------------------

    def overview(self, *, dry_run: bool, paused: bool) -> dict[str, Any]:
        hours = self.config.console.history_hours
        window = hours * 3600

        with Spool() as spool, Memory() as memory, JobStore() as jobs, EventStore() as log:
            pool = spool.stats()
            counts = log.counts(window)
            job_stats = jobs.stats()
            sends = memory.sends_in_last_hour()
            chats = jobs.chats(limit=1)
            histogram = log.histogram(window, buckets=72)

        # "Heard" is every message pooled; "spoke" is every message actually
        # sent. The gap between them is the entire point of the project.
        heard = counts.get("watchdog/spooled", 0)
        planned = counts.get("agent/planned", 0)
        spoke = counts.get("send/delivered", 0)
        drafted = counts.get("send/dry_run", 0)

        return {
            "window_hours": hours,
            "heard": heard,
            "stayed_quiet": planned,
            "spoke": spoke,
            "drafted": drafted,
            "finished": counts.get("worker/ready", 0),
            "delegated": counts.get("agent/delegated", 0),
            "histogram": histogram,
            "pool": {"pending": pool.pending, "leased": pool.leased, "dead": pool.dead},
            "jobs": {
                "queued": job_stats.queued, "running": job_stats.running,
                "ready": job_stats.ready, "delivered": job_stats.delivered,
                "failed": job_stats.failed, "expired": job_stats.expired,
                "blocked": job_stats.blocked,
            },
            "sends_last_hour": sends,
            "dry_run": dry_run,
            "paused": paused,
            "chats_seen": len(chats),
            "config_source": self.config.source,
            "model": self.config.model,
        }

    # ---- graph ------------------------------------------------------------

    def graph(self) -> dict[str, Any]:
        """The skeleton, plus a node per worker that is live or recently was."""
        nodes = [dict(n) for n in PIPELINE_NODES]
        edges = [dict(e) for e in PIPELINE_EDGES]

        with JobStore() as jobs:
            recent = jobs.recent(limit=12)

        for job in recent:
            node_id = f"worker:{job.job_key}"
            status = (
                "running" if job.state in LIVE_JOB_STATES
                else "retired" if job.state in ("expired", "failed", "blocked")
                else "done"
            )
            nodes.append({
                "id": node_id,
                "kind": f"worker · {job.kind}",
                "name": job.job_key,
                "desc": self._text(job.objective)[:110],

                "tools": ["research", "compose"],
                "status": status,
                "job": job.job_key,
                "chat": self._label(job.chat_guid),
                "state": job.state,
                "age_seconds": round(job.age, 1),
            })
            edges += [
                {"id": f"agent->{node_id}", "source": "agent", "target": node_id},
                {"id": f"{node_id}->brightdata", "source": node_id, "target": "brightdata"},
                {"id": f"{node_id}->supervisor", "source": node_id, "target": "supervisor"},
            ]

        return {"nodes": nodes, "edges": edges}

    # ---- cards ------------------------------------------------------------

    def artifacts(self, limit: int = 30) -> dict[str, Any]:
        """What the agents produced, and what they considered producing."""
        out: list[dict[str, Any]] = []

        with JobStore() as jobs:
            for job in jobs.recent(limit=limit):
                out.append({
                    "id": job.job_key,
                    "kind": "ready" if job.state in (READY, "delivered") else "retired",
                    "title": self._text(job.objective)[:80] or job.kind,
                    "agent": f"@{job.kind}",
                    "chat": self._label(job.chat_guid),
                    "state": job.state,
                    "age_seconds": round(job.age, 1),
                    "reply": self._text(job.reply),
                    "note": self._text(job.note),

                    "attempts": job.attempts,
                })

        # Proposals: what the planner thought was worth looking up. These are
        # advisory by design — a plan that named a worker is not a job, and most
        # of them never become one.
        with Memory() as memory:
            for guid in memory.chats_with_plans(limit=10):
                plan = Plan.from_json(memory.latest_plan(guid))
                if plan is None or not plan.action_warranted:
                    continue
                if plan.action_kind == "none":
                    continue
                out.append({
                    "id": f"proposal:{events_mod.anon(guid)}",
                    "kind": "proposal",
                    "title": self._text(plan.objective)[:80] or f"a {plan.action_kind} lookup",
                    "agent": f"@{plan.action_kind}",
                    "chat": self._label(guid),
                    "state": "considered",
                    "tone": self._text(plan.tone),
                    "pillar": plan.pillar,
                    "read": self._text(plan.read),
                    "missing": [
                        self._text(m) for m in (plan.action or {}).get("missing", [])
                    ],

                })

        return {"artifacts": out}

    def agents(self) -> dict[str, Any]:
        """The roster: the always-on parts, then whatever workers exist."""
        roster: list[dict[str, Any]] = [
            {"name": "@planner", "status": "always",
             "note": "Reads every group burst. Produces nothing on its own."},
            {"name": "@agent", "status": "always",
             "note": "Speaks only when named."},
        ]
        with JobStore() as jobs:
            for job in jobs.recent(limit=12):
                status = (
                    "running" if job.state in LIVE_JOB_STATES
                    else "retired" if job.state in ("expired", "failed", "blocked")
                    else "dormant"
                )
                roster.append({
                    "name": f"@{job.kind}:{job.job_key}",
                    "status": status,
                    "note": f"{job.state} · {job.age:.0f}s ago · {self._text(job.objective)[:60]}",

                })
        return {"agents": roster}

    def dossier(self, chat_key: str) -> dict[str, Any] | None:
        """What the agent knows about one chat, keyed the way the log keys it."""
        with Memory() as memory:
            target = next(
                (g for g in memory.chats_with_plans(limit=100)
                 if events_mod.anon(g) == chat_key or g == chat_key),
                None,
            )
            if target is None:
                return None
            known = Dossier.from_json(memory.load_dossier(target))
            return {
                "chat": self._label(target),
                "vibe": known.vibe,
                "occasion": known.occasion,
                "updated_at": known.updated_at,
                "people": [
                    {
                        "who": self._label(p.handle),
                        "name": p.name,
                        "location": p.location,
                        "availability": p.availability,
                        "notes": p.notes,
                    }
                    for p in known.people.values()
                ],
            }

    # ---- resolving a chat from what the UI was shown ----------------------

    def resolve_chat(self, chat_key: str) -> str | None:
        """Turn the key the UI holds back into a real chat guid.

        The console shows anonymised keys, so a control action arrives naming a
        hash rather than a conversation. Resolving happens here, server-side,
        which also means the UI can never name a chat the backend has not
        already seen.
        """
        with Memory() as memory, JobStore() as jobs:
            candidates = list(memory.chats_with_plans(limit=200)) + list(jobs.chats(limit=200))
        for guid in candidates:
            if guid == chat_key or events_mod.anon(guid) == chat_key:
                return guid
        return None

    # ---- log --------------------------------------------------------------


    def log(self, limit: int = 100, stage: str | None = None) -> dict[str, Any]:
        with EventStore() as store:
            return {"log": store.recent(limit, stage=stage), "cursor": store.max_id()}
