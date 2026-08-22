"""The supervisor agent server: drain the pool, decide, reply, acknowledge.

Independent of the watchdog. It never opens chat.db and holds no Full Disk
Access requirement — it reads work from the shared pool and writes replies
through Messages.app. Stopping it does not stop message capture; the pool keeps
filling and this server catches up when it returns.

Failure handling follows the pool's lease semantics:

* handled, skipped, or blocked → terminal, acknowledged
* transient failure (API error, send failure) → returned for retry
* repeated failure → dead-lettered rather than retried forever
"""

from __future__ import annotations

import signal
import socket
import threading
import time
from dataclasses import asdict, dataclass

from plug import events
from plug.config import Config
from plug.safety import ReplyPolicy
from plug.spool import Spool

from .agent import Agent
from .buffer import Buffer, WorkBatch
from .memory import Memory

STAGE = "supervisor"


@dataclass(slots=True)
class SupervisorStats:
    ticks: int = 0
    leased: int = 0
    filtered: int = 0
    batches: int = 0
    sent: int = 0
    skipped: int = 0
    blocked: int = 0
    retried: int = 0
    failed: int = 0
    planned: int = 0


class SupervisorServer:
    def __init__(
        self,
        config: Config,
        spool: Spool,
        memory: Memory,
        policy: ReplyPolicy,
        agent: Agent,
        *,
        echo: bool = False,
        owner: str | None = None,
    ) -> None:
        self.config = config
        self.spool = spool
        self.memory = memory
        self.policy = policy
        self.agent = agent
        self.buffer = Buffer(config)
        self.echo = echo
        # Identifies which supervisor holds a lease, so a stuck one is traceable.
        self.owner = owner or f"{socket.gethostname()}:{int(time.time())}"
        self.stats = SupervisorStats()
        self._stop = False
        # Set by the watchdog's /notify ping (and by request_stop). The loop
        # waits on this instead of sleeping blind, so a pooled message is picked
        # up in milliseconds rather than after a full idle interval.
        self._wake = threading.Event()

    # ---- lifecycle --------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        self._stop = True
        # Cut the wait short so shutdown doesn't sit out the idle interval.
        self._wake.set()

    def notify(self, *_: object) -> None:
        """Wake the loop now — the watchdog just pooled something.

        Idempotent and cheap: the loop drains everything pending when it wakes,
        so extra pings are harmless and a missed one only costs latency.
        """
        self._wake.set()

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

    def _intake(self) -> int:
        """Lease pending work and apply reply policy. Returns items buffered."""
        cfg = self.config.supervisor
        items = self.spool.lease(
            self.owner, limit=cfg.lease_limit, lease_seconds=cfg.lease_seconds
        )
        if not items:
            return 0

        self.stats.leased += len(items)
        buffered = 0
        for item in items:
            verdict = self.policy.should_reply(item.message)
            if verdict:
                self.buffer.add(item)
                buffered += 1
            else:
                self.stats.filtered += 1
                self.spool.drop([item.id], verdict.reason)
                events.emit(
                    STAGE, "filtered", chat=item.message.chat_guid,
                    reason=verdict.reason, echo=self.echo,
                )
        return buffered

    def _dispatch(self, work: WorkBatch) -> None:
        self.stats.batches += 1
        cfg = self.config.supervisor

        # Re-check just before the model call: a burst may have sat through the
        # debounce window while a reply went out or the kill switch flipped.
        verdict = self.policy.should_reply(work.batch.last)
        if not verdict:
            self.stats.filtered += 1
            self.spool.drop(work.ids, verdict.reason)
            events.emit(STAGE, "dropped", chat=work.chat_guid, reason=verdict.reason, echo=self.echo)
            return

        events.emit(STAGE, "dispatch", chat=work.chat_guid, size=len(work.ids), echo=self.echo)
        outcome = self.agent.handle(work.batch)

        if outcome.errors:
            detail = "; ".join(outcome.errors)
            if outcome.permanent:
                # Bad request, auth, or billing. Another attempt fails identically,
                # so settle it now and surface the reason rather than burning retries.
                self.stats.failed += 1
                self.spool.drop(work.ids, f"permanent failure: {detail}")
                events.emit(STAGE, "permanent_failure", chat=work.chat_guid, error=detail, echo=self.echo)
            else:
                self.stats.retried += 1
                self.spool.nack(work.ids, detail, max_attempts=cfg.max_attempts)
                events.emit(STAGE, "retry", chat=work.chat_guid, attempts=work.max_attempts, echo=self.echo)
            return

        if outcome.planned:
            # Read the room and stayed quiet: the normal group-chat outcome.
            # Terminal — the plan is already stored, so there is nothing to retry.
            self.stats.planned += 1
            self.spool.drop(work.ids, outcome.reason or "planned, not addressed")
        elif outcome.sent:
            self.stats.sent += 1
            self.spool.ack(work.ids, "replied")
        elif outcome.skipped:
            self.stats.skipped += 1
            self.spool.drop(work.ids, f"agent skipped: {outcome.reason}")
        elif outcome.blocked:
            # Safety declined. A rate-limited reply delivered an hour late reads
            # worse than none at all, so this is terminal rather than retried.
            self.stats.blocked += 1
            self.spool.drop(work.ids, f"blocked: {outcome.reason}")
        else:
            self.stats.skipped += 1
            self.spool.drop(work.ids, outcome.reason or "no action")

    def tick(self) -> bool:
        """One pass. Returns True if there was anything to do."""
        self.stats.ticks += 1
        buffered = self._intake()
        due = self.buffer.due()
        for work in due:
            self._dispatch(work)
        return bool(buffered or due)

    def run(self, *, handle_signals: bool = True) -> SupervisorStats:
        if handle_signals:
            self.install_signal_handlers()
        events.emit(
            STAGE, "started", owner=self.owner, dry_run=self.agent.dry_run,
            model=self.config.model, echo=self.echo,
        )

        idle = self.config.supervisor.idle_sleep_seconds
        while not self._stop:
            # Clear before working, not after: a ping that lands while tick() is
            # running then survives to the wait below instead of being lost.
            self._wake.clear()

            try:
                busy = self.tick()
            except Exception as exc:  # keep the server alive through transient faults
                events.emit(STAGE, "tick_error", error=repr(exc), echo=self.echo)
                busy = False

            if self._stop:
                break

            # Straight back round if there was work. If a burst is mid-debounce,
            # check again promptly. Otherwise wait for an alert, with the idle
            # interval as the backstop that makes notification optional.
            if busy:
                continue
            timeout = 0.25 if self.buffer.pending_chats() else idle
            self._wake.wait(timeout)

        self._shutdown()
        events.emit(STAGE, "stopped", **asdict(self.stats), echo=self.echo)
        return self.stats

    def _shutdown(self) -> None:
        """Hand back anything still buffered instead of holding it until lease expiry."""
        leftover = self.buffer.drain()
        if not leftover:
            return
        ids = [i for work in leftover for i in work.ids]
        self.spool.nack(ids, "supervisor shut down", max_attempts=self.config.supervisor.max_attempts)
        events.emit(STAGE, "released_on_shutdown", count=len(ids), echo=self.echo)
