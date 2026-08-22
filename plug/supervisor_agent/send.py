"""Deliver replies through Messages.app via AppleScript.

Security note: message bodies are attacker-controlled text that ends up in an
AppleScript program. They are passed as ``osascript`` **arguments** and read via
``on run argv``, never interpolated into script source. Building the script by
string concatenation would let an inbound message containing a quote plus
``do shell script`` run arbitrary code on this machine.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "applescript"

SEND_TO_CHAT = SCRIPT_DIR / "send_to_chat.applescript"
SEND_TO_PARTICIPANT = SCRIPT_DIR / "send_to_participant.applescript"
SEND_TO_BUDDY = SCRIPT_DIR / "send_to_buddy.applescript"

# osascript can hang if Messages.app is wedged; never block the poll loop forever.
TIMEOUT_SECONDS = 30


class SendError(RuntimeError):
    """Every addressing strategy failed."""


@dataclass(frozen=True, slots=True)
class SendResult:
    strategy: str
    ok: bool
    detail: str = ""


def _run(script: Path, *args: str) -> SendResult:
    try:
        proc = subprocess.run(
            ["osascript", str(script), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return SendResult(script.stem, False, "osascript timed out")
    if proc.returncode == 0:
        return SendResult(script.stem, True)
    return SendResult(script.stem, False, (proc.stderr or proc.stdout).strip())


def deliver(text: str, chat_guid: str, handle: str | None, service: str) -> SendResult:
    """Send ``text`` to a chat, trying each addressing strategy in turn.

    Strategy order is the outcome of the Phase 0 spike: AppleScript's ``chat id``
    values match ``chat.guid`` from the database verbatim on macOS 26, so the
    direct chat address is tried first. The participant and buddy forms remain as
    fallbacks for chats Messages won't resolve by guid.
    """
    attempts: list[SendResult] = []

    result = _run(SEND_TO_CHAT, text, chat_guid)
    attempts.append(result)
    if result.ok:
        return result

    if handle:
        # `service type` expects iMessage or SMS; RCS chats are reachable through
        # the SMS account.
        svc = "iMessage" if service == "iMessage" else "SMS"
        result = _run(SEND_TO_PARTICIPANT, text, handle, svc)
        attempts.append(result)
        if result.ok:
            return result

        result = _run(SEND_TO_BUDDY, text, handle)
        attempts.append(result)
        if result.ok:
            return result

    detail = "; ".join(f"{a.strategy}: {a.detail}" for a in attempts)
    raise SendError(f"all send strategies failed -> {detail}")
