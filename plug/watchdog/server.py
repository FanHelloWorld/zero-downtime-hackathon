"""The poll loop: every N seconds, read chat.db and pool new messages to disk.

This process is deliberately dumb. It does not debounce, call a model, or send
anything — it only moves rows from Apple's database into ours. That keeps the
component that holds Full Disk Access as small and auditable as possible, and it
means the pool keeps filling even while the supervisor agent is stopped.
"""

from __future__ import annotations

import signal
import time
from dataclasses import asdict, dataclass

from plug import events
from plug.config import Config
from plug.notify import Notifier
from plug.safety import IngestFilter
from plug.spool import Spool

from .db import ChatDB
from .state import WatchdogState

STAGE = "watchdog"


@dataclass(slots=True)
class WatchdogStats:
    ticks: int = 0
    seen: int = 0
    spooled: int = 0
    filtered: int = 0
    duplicates: int = 0


class WatchdogServer:
    def __init__(
        self,
        config: Config,
        db: ChatDB,
        state: WatchdogState,
        spool: Spool,
        *,
        echo: bool = False,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.state = state
        self.spool = spool
        self.filter = IngestFilter(config)
        self.notifier = notifier or Notifier(
            config.watchdog.notify_url, timeout=config.watchdog.notify_timeout_seconds
        )
        self.echo = echo
        self.stats = WatchdogStats()
        self._stop = False
        self._last_purge = 0.0

    # ---- lifecycle --------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def install_signal_handlers(self) -> bool:
        """Handle Ctrl-C when we own the process. Returns False if we don't.

        Under uvicorn the loop runs in a worker thread, where signal.signal
        raises ValueError — and the ASGI lifespan owns shutdown anyway.
        """
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
            return True
        except ValueError:
            return False

    # ---- work -------------------------------------------------------------

    def tick(self) -> int:
        """One poll. Returns how many messages were newly pooled."""
        self.stats.ticks += 1
        cursor = self.state.get_cursor() or 0
        new = self.db.messages_after(cursor, limit=self.config.watchdog.read_limit)
        if not new:
            return 0

        self.stats.seen += len(new)

        keep = []
        for message in new:
            verdict = self.filter.should_ingest(message)
            if verdict:
                keep.append(message)
            else:
                self.stats.filtered += 1
                events.emit(
                    STAGE, "filtered", chat=message.chat_guid,
                    rowid=message.rowid, reason=verdict.reason, echo=self.echo,
                )

        spooled = self.spool.enqueue(keep)
        duplicates = len(keep) - spooled
        self.stats.spooled += spooled
        self.stats.duplicates += duplicates

        # Advance past everything examined, including filtered rows — otherwise
        # a single short-code message is re-read on every tick forever.
        high_water = max(m.rowid for m in new)
        self.state.set_cursor(high_water)

        if spooled:
            events.emit(
                STAGE, "spooled", count=spooled, cursor=high_water,
                depth=self.spool.stats().depth, echo=self.echo,
            )
            # Alert the agent rather than making it wait out its idle sleep.
            # Fire-and-forget: this never blocks or fails the poll loop.
            self.notifier.ping(spooled)
        if duplicates:
            # Expected after a restart that re-reads a window; worth seeing, not alarming.
            events.emit(STAGE, "duplicates_ignored", count=duplicates, echo=self.echo)

        return spooled

    def _maybe_purge(self) -> None:
        """Housekeeping, at most hourly, so settled rows don't accumulate forever."""
        now = time.monotonic()
        if now - self._last_purge < 3600:
            return
        self._last_purge = now
        removed = self.spool.purge(self.config.watchdog.purge_after_days * 24 * 3600)
        if removed:
            events.emit(STAGE, "purged", count=removed, echo=self.echo)

    def run(self, *, handle_signals: bool = True) -> WatchdogStats:
        if handle_signals:
            self.install_signal_handlers()
        interval = self.config.watchdog.poll_interval_seconds
        events.emit(
            STAGE, "started", interval=interval, spool=str(self.spool.path),
            notify=self.notifier.url if self.notifier.enabled else None, echo=self.echo,
        )
        self._last_purge = time.monotonic()

        while not self._stop:
            started = time.monotonic()
            try:
                self.tick()
                self._maybe_purge()
            except Exception as exc:  # keep the server alive through transient faults
                events.emit(STAGE, "tick_error", error=repr(exc), echo=self.echo)

            remaining = interval - (time.monotonic() - started)
            # Sleep in slices so Ctrl-C is responsive rather than waiting a full tick.
            while remaining > 0 and not self._stop:
                nap = min(0.25, remaining)
                time.sleep(nap)
                remaining -= nap

        events.emit(STAGE, "stopped", **asdict(self.stats), echo=self.echo)
        return self.stats
