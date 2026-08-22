"""The supervisor agent server.

An independent process that leases messages from the shared disk pool, decides
whether and how to reply, and delivers through Messages.app. It never opens
chat.db, so it needs no Full Disk Access — only Automation permission and an API
key. Stopping it does not stop message capture.
"""

from .server import SupervisorServer, SupervisorStats

__all__ = ["SupervisorServer", "SupervisorStats"]
