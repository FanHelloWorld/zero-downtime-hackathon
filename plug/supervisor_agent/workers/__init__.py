"""Background agents the supervisor dispatches when a reply needs a lookup.

Supervisor-side only. Nothing here is importable from ``watchdog/``, and nothing
here sends a message — a worker produces text and stops. Delivery happens on the
supervisor's loop thread, through the same safety gate as any other reply.
"""

from .registry import WorkerSpec, get, kinds
from .runner import WorkerPool

__all__ = ["WorkerSpec", "WorkerPool", "get", "kinds"]
