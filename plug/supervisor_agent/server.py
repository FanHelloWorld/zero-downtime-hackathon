"""The supervisor agent server: drain the pool, decide, reply, acknowledge.

Independent of the watchdog. It never opens chat.db and holds no Full Disk
Access requirement — it reads work from the shared pool and writes replies
through Messages.app. Stopping it does not stop message capture; the pool keeps
filling and this server catches up when it returns.

Failure handling follows the pool's lease semantics:

* handled, skipped, or blocked → terminal, acknowledged
* transient failure (API error, send failure) → returned for retry
* repeated failure → dead-lettered rather than retried forever

Delegated work is the one thing that outlives a batch. When the agent promises to
look something up, the pool item is settled immediately and the promise moves to
the job store; the loop then starts worker threads and, later, delivers what they
found. Delivery happens here rather than on the worker thread on purpose — every
message this system sends leaves from the loop, through one gate.
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

from . import send
from .agent import Agent
from .buffer import Buffer, WorkBatch
from .jobs import BLOCKED, DELIVERED, JobStore
from .memory import Memory
from .workers import WorkerPool


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
    delegated: int = 0
    jobs_started: int = 0
    follow_ups: int = 0
    follow_ups_blocked: int = 0


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
        jobs: JobStore | None = None,
        workers: WorkerPool | None = None,
    ) -> None:
        self.config = config
        self.spool = spool
        self.memory = memory
        self.policy = policy
        self.agent = agent
        self.buffer = Buffer(config)
        self.echo = echo
        # Both optional: a supervisor with workers switched off is a valid
        # configuration, and the reply path does not depend on them.
        self.jobs = jobs
        self.workers = workers

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
        elif outcome.delegated:
            # Said something and filed a job. Terminal here: the promise now lives
            # in the job store, and retrying the batch would file it twice.
            self.stats.delegated += 1
            self.spool.ack(work.ids, f"delegated {outcome.job_key}")
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
            # The model answered in prose instead of calling a tool, so nothing
            # was sent. Retry rather than drop: dropping makes this look exactly
            # like a deliberate silence, and it is the one failure a person in
            # the chat would notice and we would not. Out of attempts, it
            # dead-letters, where `plug-supervisor dead` can show it.
            self.stats.retried += 1
            self.spool.nack(
                work.ids, outcome.reason or "no action", max_attempts=cfg.max_attempts
            )
            events.emit(
                STAGE, "no_tool_call", chat=work.chat_guid,
                reason=outcome.reason, attempts=work.max_attempts, echo=self.echo,
            )


    # ---- delegated work ---------------------------------------------------

    def _pump_workers(self) -> int:
        """Start whatever background work there is room for. Never raises."""
        if self.workers is None:
            return 0
        try:
            started = self.workers.pump()
        except Exception as exc:
            # A worker pool that cannot start jobs must not stop replies.
            events.emit(STAGE, "worker_pump_error", error=repr(exc), echo=self.echo)
            return 0
        self.stats.jobs_started += started
        return started

    def _deliver_ready(self) -> int:
        """Send the follow-ups workers have finished writing.

        The loop thread does this, not the worker, so that every outbound message
        passes the same gate. ``follow_up=True`` relaxes exactly one check — the
        loop guard, which a worker finishing inside its window would otherwise
        trip after the chat was told to expect an answer. Pause, rate limits and
        the rest still apply, which is why a job can still end up blocked here.
        """
        if self.jobs is None:
            return 0

        delivered = 0
        for job in self.jobs.deliverable(limit=5):
            verdict = self.policy.can_send(
                job.chat_guid, job.reply, is_group=job.is_group, follow_up=True
            )
            if not verdict:
                self.stats.follow_ups_blocked += 1
                self.jobs.settle(job.id, BLOCKED, verdict.reason)
                events.emit(
                    "safety", "send_blocked", chat=job.chat_guid,
                    reason=verdict.reason, job=job.job_key, follow_up=True, echo=self.echo,
                )
                continue

            if self.agent.dry_run:
                self.memory.record_send(job.chat_guid, job.reply, dry_run=True)
                self.jobs.settle(job.id, DELIVERED, "dry run")
                events.emit(
                    "send", "dry_run", chat=job.chat_guid, job=job.job_key,
                    chars=len(job.reply), would_send=job.reply, follow_up=True,
                    note="AppleScript NOT invoked — unset PLUG_DRY_RUN to send for real",
                    echo=self.echo,
                )
                delivered += 1
                continue

            try:
                result = send.deliver(job.reply, job.chat_guid, job.handle, job.service)
            except send.SendError as exc:
                # Terminal, like a blocked reply. Every addressing strategy has
                # already failed, so Messages.app is wedged rather than busy —
                # retrying each tick would spin, not recover.
                self.stats.follow_ups_blocked += 1
                self.jobs.settle(job.id, BLOCKED, f"send failed: {exc}")
                events.emit(
                    "send", "failed", chat=job.chat_guid, job=job.job_key,
                    error=str(exc), follow_up=True, echo=self.echo,
                )
                continue

            self.stats.follow_ups += 1
            self.memory.record_send(job.chat_guid, job.reply, dry_run=False)
            self.memory.append_history(job.chat_guid, "assistant", job.reply)
            self.jobs.settle(job.id, DELIVERED, result.strategy)
            events.emit(
                "send", "delivered", chat=job.chat_guid, job=job.job_key,
                strategy=result.strategy, chars=len(job.reply), follow_up=True,
                echo=self.echo,
            )
            delivered += 1
        return delivered

    def tick(self) -> bool:
        """One pass. Returns True if there was anything to do."""
        self.stats.ticks += 1
        buffered = self._intake()
        due = self.buffer.due()
        for work in due:
            self._dispatch(work)
        started = self._pump_workers()
        delivered = self._deliver_ready()
        return bool(buffered or due or started or delivered)


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
        if self.workers is not None:
            # Stop claiming, then hand back what this owner still holds. Threads
            # already running are daemons and will be killed with the process;
            # their jobs return to 'queued' and are re-run on the next start
            # rather than sitting in 'running' until the lease lapses.
            self.workers.request_stop()
            if self.jobs is not None:
                released = self.jobs.release(self.workers.owner)
                if released:
                    events.emit(STAGE, "jobs_released", count=released, echo=self.echo)

        leftover = self.buffer.drain()
        if not leftover:
            return
        ids = [i for work in leftover for i in work.ids]
        self.spool.nack(ids, "supervisor shut down", max_attempts=self.config.supervisor.max_attempts)
        events.emit(STAGE, "released_on_shutdown", count=len(ids), echo=self.echo)

