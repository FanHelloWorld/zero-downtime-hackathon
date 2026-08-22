"""Guards, split along the same seam as the two servers.

``IngestFilter`` is stateless and runs in the watchdog: it decides what is even
worth spooling. ``ReplyPolicy`` runs in the supervisor agent and needs the send
log, so it lives on the side that owns that history.

Splitting them this way means policy changes — rate limits, deny patterns, the
kill switch — take effect by restarting the supervisor alone. The watchdog keeps
pooling to disk throughout.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .config import PAUSE_FILE, Config
from .models import Message


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Verdict(True)


class IngestFilter:
    """Watchdog-side. Structural only — no state, no history, no network."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def should_ingest(self, message: Message) -> Verdict:
        chats = self.config.chats

        if not message.body.strip():
            return Verdict(False, "empty body")
        if message.is_group and not chats.include_groups:
            return Verdict(False, "group chats disabled")
        if not message.is_group and not chats.include_1to1:
            return Verdict(False, "1:1 chats disabled")
        if chats.services and message.service not in chats.services:
            return Verdict(False, f"service {message.service} not enabled")

        return ALLOW


class ReplyPolicy:
    """Supervisor-side. Everything that needs send history or the kill switch."""

    def __init__(self, config: Config, memory) -> None:
        self.config = config
        self.memory = memory
        self._deny = [re.compile(p, re.IGNORECASE) for p in config.safety.deny_patterns]

    # ---- before the agent sees it ----------------------------------------

    def should_reply(self, message: Message) -> Verdict:
        chats = self.config.chats
        safety = self.config.safety

        handle = message.handle or ""
        if chats.only_handles and handle not in chats.only_handles:
            return Verdict(False, "handle not in only_handles")
        if handle in safety.never_reply_to:
            return Verdict(False, "handle in never_reply_to")

        # All-digit senders with few digits are short codes: banks, 2FA robots,
        # delivery notifications. Replying to them is noise at best.
        bare = handle.lstrip("+")
        if bare.isdigit() and len(bare) <= safety.shortcode_max_digits:
            return Verdict(False, "sender looks like a short code")

        for pattern in self._deny:
            if pattern.search(message.body):
                return Verdict(False, "matched deny pattern")

        # Loop guard: if we just sent to this chat, an immediate inbound message
        # may be our own text echoing back through another device, or another
        # bot answering us. Two of these left running would ping-pong forever.
        last = self.memory.last_send_ts(message.chat_guid)
        if last is not None and (time.time() - last) < safety.loop_window_seconds:
            return Verdict(False, "inside loop-guard window")

        return ALLOW

    # ---- immediately before delivery --------------------------------------

    def can_send(self, chat_guid: str, text: str) -> Verdict:
        """Final gate. Called from inside the agent's send tool."""
        safety = self.config.safety

        if PAUSE_FILE.exists():
            return Verdict(False, "paused (kill switch active)")

        if not text or not text.strip():
            return Verdict(False, "empty reply")
        if len(text) > safety.max_reply_chars:
            return Verdict(False, f"reply exceeds {safety.max_reply_chars} chars")

        for pattern in self._deny:
            if pattern.search(text):
                return Verdict(False, "reply matched deny pattern")

        per_chat = self.memory.sends_in_last_hour(chat_guid)
        if per_chat >= safety.per_chat_per_hour:
            return Verdict(False, f"per-chat rate limit ({safety.per_chat_per_hour}/hr)")

        overall = self.memory.sends_in_last_hour()
        if overall >= safety.global_per_hour:
            return Verdict(False, f"global rate limit ({safety.global_per_hour}/hr)")

        return ALLOW


def pause() -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.touch()


def resume() -> None:
    PAUSE_FILE.unlink(missing_ok=True)


def is_paused() -> bool:
    return PAUSE_FILE.exists()
