"""Domain objects shared across the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Apple's Core Data epoch is 2001-01-01 UTC; message.date is nanoseconds since.
APPLE_EPOCH_OFFSET = 978_307_200

# chat.style values observed in chat.db.
STYLE_GROUP = 43
STYLE_DIRECT = 45


def apple_time_to_datetime(raw: int | None) -> datetime:
    """Convert a chat.db ``date`` value to an aware UTC datetime."""
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(raw / 1_000_000_000 + APPLE_EPOCH_OFFSET, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class Message:
    """One inbound message, already resolved to readable text."""

    rowid: int
    guid: str
    body: str
    sent_at: datetime
    handle: str | None
    """Sender's phone number or email. None on some system-originated rows."""
    chat_guid: str
    chat_identifier: str
    service: str
    """iMessage, SMS, or RCS — taken from chat.service_name."""
    style: int
    display_name: str | None

    @property
    def is_group(self) -> bool:
        return self.style == STYLE_GROUP

    @property
    def chat_label(self) -> str:
        """Human-readable chat name for logs and agent context."""
        return self.display_name or self.chat_identifier


@dataclass(slots=True)
class Batch:
    """A debounced burst of messages from one chat, handed to the agent as one turn."""

    chat_guid: str
    messages: list[Message]

    @property
    def last(self) -> Message:
        return self.messages[-1]

    @property
    def is_group(self) -> bool:
        return self.last.is_group

    @property
    def service(self) -> str:
        return self.last.service

    def transcript(self) -> str:
        """Render the burst for the agent prompt, attributing each line."""
        lines = []
        for m in self.messages:
            who = m.handle or "unknown"
            lines.append(f"{who}: {m.body}" if self.is_group else m.body)
        return "\n".join(lines)
