"""Watchdog → supervisor wake-up.

Polling alone means a message waits out the supervisor's idle sleep before
anyone looks at it. This closes that gap: the moment the watchdog pools
something, it pings the supervisor's ``/notify`` endpoint and the drain loop
wakes immediately.

The ping is strictly an optimisation. It carries no message data, and the
supervisor still polls on its own timer, so a lost, refused, or slow ping costs
latency and nothing else. That matters because the two servers are meant to be
independently restartable — the watchdog must never block or fail because the
agent is down.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from . import events

STAGE = "notify"


class Notifier:
    """Fire-and-forget HTTP ping, off the poll loop's thread."""

    def __init__(self, url: str | None, *, timeout: float = 2.0) -> None:
        self.url = url
        self.timeout = timeout
        self._inflight = threading.Lock()
        self._failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def ping(self, count: int) -> None:
        """Ask the supervisor to wake up. Never raises, never blocks."""
        if not self.url:
            return

        # Coalesce: one wake-up drains everything pending, so a second ping
        # while the first is in flight would tell the supervisor nothing new.
        if not self._inflight.acquire(blocking=False):
            return

        thread = threading.Thread(
            target=self._send, args=(count,), name="plug-notify", daemon=True
        )
        thread.start()

    def _send(self, count: int) -> None:
        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps({"spooled": count}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
            if self._failures:
                events.emit(STAGE, "recovered", after_failures=self._failures)
            self._failures = 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._failures += 1
            # A stopped supervisor would otherwise log every few seconds
            # forever. First failure, then exponentially sparser.
            if self._failures == 1 or self._failures % 20 == 0:
                events.emit(
                    STAGE, "failed",
                    error=repr(exc), consecutive=self._failures,
                    note="supervisor will still pick this up by polling",
                )
        finally:
            self._inflight.release()
