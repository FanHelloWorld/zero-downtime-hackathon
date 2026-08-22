"""The console: a third server whose only job is to be looked at.

It reads the state directory and serves a UI. What it deliberately cannot do is
as important as what it can: no chat.db, so no Full Disk Access; no AppleScript,
so no Automation permission; no model, so no API key. The watchdog can only
read, the supervisor can only send, and this one can only show — the same
least-privilege split, extended to a third member.

The controls it does expose (pause, resume, cancel a lookup, start one) all go
through the same gates the rest of the system uses. None of them is a new way to
send a message.
"""

from .server import ConsoleData

__all__ = ["ConsoleData"]
