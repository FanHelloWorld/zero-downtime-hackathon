"""Coalesce leased pool items so one thought produces one reply.

People text in bursts: "hey", "you around?", "need to ask you something". Firing
the agent three times produces three replies and reads as a malfunction. The
buffer holds a chat's items until it has been quiet for ``debounce_seconds`` (or
the burst reaches ``max_batch``), then releases them together.

Debouncing lives here rather than in the watchdog on purpose: the watchdog's job
is to get messages onto disk as fast as possible, and holding them in memory to
wait out a burst would put unpooled data at risk of a crash.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from plug.config import Config
from plug.models import Batch
from plug.spool import SpoolItem


@dataclass(slots=True)
class WorkBatch:
    """A debounced burst, carrying the pool ids needed to acknowledge it."""

    batch: Batch
    ids: list[int]
    max_attempts: int

    @property
    def chat_guid(self) -> str:
        return self.batch.chat_guid


class Buffer:
    def __init__(self, config: Config) -> None:
        self.config = config
        # Ordered by first-seen so bursts are released roughly in arrival order.
        self._pending: OrderedDict[str, list[SpoolItem]] = OrderedDict()
        self._last_seen: dict[str, float] = {}

    def add(self, item: SpoolItem) -> None:
        self._pending.setdefault(item.message.chat_guid, []).append(item)
        self._last_seen[item.message.chat_guid] = time.monotonic()

    def pending_chats(self) -> int:
        return len(self._pending)

    def pending_items(self) -> int:
        return sum(len(v) for v in self._pending.values())

    def _make(self, chat_guid: str, items: list[SpoolItem]) -> WorkBatch:
        return WorkBatch(
            batch=Batch(chat_guid=chat_guid, messages=[i.message for i in items]),
            ids=[i.id for i in items],
            max_attempts=max(i.attempts for i in items),
        )

    def due(self, now: float | None = None) -> list[WorkBatch]:
        """Release batches that have gone quiet or hit the size cap."""
        now = time.monotonic() if now is None else now
        ready: list[WorkBatch] = []
        cfg = self.config.supervisor

        for chat_guid in list(self._pending.keys()):
            items = self._pending[chat_guid]
            quiet_for = now - self._last_seen.get(chat_guid, now)
            if quiet_for >= cfg.debounce_seconds or len(items) >= cfg.max_batch:
                ready.append(self._make(chat_guid, items))
                del self._pending[chat_guid]
                self._last_seen.pop(chat_guid, None)

        return ready

    def drain(self) -> list[WorkBatch]:
        """Release everything regardless of timing — used on shutdown.

        Anything still held here has an active pool lease, so a hard kill is
        recoverable too: the lease expires and the next supervisor retakes it.
        """
        batches = [self._make(g, items) for g, items in self._pending.items()]
        self._pending.clear()
        self._last_seen.clear()
        return batches
