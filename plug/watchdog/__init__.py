"""The watchdog server.

An independent process whose entire job is: read ``chat.db`` on a timer, and
append anything new to the shared disk pool. It never calls a model, never sends
a message, and never needs an API key — so it can run continuously and be
restarted freely without touching the reply path.
"""

from .server import WatchdogServer, WatchdogStats

__all__ = ["WatchdogServer", "WatchdogStats"]
