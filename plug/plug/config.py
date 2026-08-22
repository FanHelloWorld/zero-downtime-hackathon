"""Configuration shared by both servers.

One file configures both processes. Each reads the whole thing but only acts on
its own section, so there is a single source of truth for the message filters
that the watchdog and the supervisor agent must agree on.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

STATE_DIR = Path(os.path.expanduser("~/.plug"))
PAUSE_FILE = STATE_DIR / "PAUSE"

# The disk pool both servers share. This is the only thing they have in common.
SPOOL_DB = STATE_DIR / "spool.db"

# Private per-server state. The watchdog owns its chat.db cursor; the supervisor
# owns conversation history and the send log. Neither reads the other's file.
WATCHDOG_DB = STATE_DIR / "watchdog.db"
SUPERVISOR_DB = STATE_DIR / "supervisor.db"

EVENT_LOG = STATE_DIR / "events.jsonl"
CHAT_DB = Path(os.path.expanduser("~/Library/Messages/chat.db"))

DEFAULT_PERSONA = """You are Plug, replying to text messages on Manjesh's behalf.
Keep replies short and natural — one or two sentences, the way a person texts.
Match the tone of the conversation."""


class WatchdogConfig(BaseModel):
    """The disk-polling server."""

    poll_interval_seconds: float = 3.0
    read_limit: int = 200
    """Maximum chat.db rows pulled per tick."""
    purge_after_days: float = 7.0
    """Settled spool rows older than this are deleted during housekeeping."""
    notify_url: str | None = "http://127.0.0.1:8002/notify"
    """Supervisor endpoint pinged the moment messages are pooled.

    Purely a latency optimisation — the supervisor also polls, so a supervisor
    that is down, moved, or running from the CLI (no HTTP) simply picks the work
    up on its next poll. Set to null to disable.
    """
    notify_timeout_seconds: float = 2.0


class SupervisorConfig(BaseModel):
    """The agent server that drains the pool."""

    lease_seconds: float = 120.0
    """How long a leased item stays claimed before another consumer may retake it."""
    lease_limit: int = 50
    idle_sleep_seconds: float = 1.0
    """Pause between polls of the pool when it is empty."""
    debounce_seconds: float = 4.0
    """Quiet period before a chat's burst is handed to the agent."""
    max_batch: int = 10
    max_attempts: int = 3
    """Attempts before an item is dead-lettered instead of retried forever."""


class ChatFilter(BaseModel):
    include_groups: bool = True
    include_1to1: bool = True
    services: list[str] = Field(default_factory=lambda: ["iMessage", "SMS", "RCS"])
    only_handles: list[str] = Field(default_factory=list)
    """When non-empty, act on these handles only. Everything else is ignored."""


class SafetyConfig(BaseModel):
    per_chat_per_hour: int = 6
    global_per_hour: int = 30
    loop_window_seconds: int = 30
    max_reply_chars: int = 600
    deny_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\b\d{4,8}\b.{0,40}\b(code|verification|verify|OTP|passcode|one[- ]time)\b",
            r"\b(code|verification|verify|OTP|passcode|one[- ]time)\b.{0,40}\b\d{4,8}\b",
            r"\bdo not share\b",
        ]
    )
    never_reply_to: list[str] = Field(default_factory=list)
    shortcode_max_digits: int = 6
    """Senders that are all-digits and no longer than this are treated as robots."""


class Config(BaseModel):
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)

    model: str = "claude-haiku-4-5"
    max_tokens: int = 1024
    persona: str = DEFAULT_PERSONA
    history_turns: int = 12

    chats: ChatFilter = Field(default_factory=ChatFilter)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    source: str = "defaults"
    """Where this config came from, so "which config am I running?" is answerable.

    Both ASGI apps load config at import time, so a server started from the
    wrong working directory silently falls back to defaults. Surfacing the
    resolved path in /status turns that from a mystery into a glance.
    """

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        candidate = Path(path) if path else Path("plug.yml")
        if not candidate.exists():
            return cls(source=f"defaults (no {candidate})")
        data = yaml.safe_load(candidate.read_text()) or {}
        config = cls.model_validate(data)
        config.source = str(candidate.resolve())
        return config


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a .env file, without overriding real env vars.

    Kept deliberately small — the only secret Plug needs is ANTHROPIC_API_KEY,
    and it must never live in plug.yml, which is meant to be shareable.
    """
    candidate = Path(path)
    if not candidate.exists():
        return
    for raw in candidate.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
