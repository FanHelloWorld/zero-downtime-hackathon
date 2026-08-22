"""Run a blocking server loop underneath an ASGI lifespan.

Both server loops are synchronous: they sleep, and they talk to SQLite. Running
either directly on the event loop would block every HTTP request for the whole
poll interval, so each runs on its own thread and the ASGI app keeps serving.

The important detail is *where* resources are built. SQLite connections belong to
the thread that opened them, so ``build`` is invoked inside the worker thread
rather than at lifespan setup. HTTP handlers therefore must not touch the loop's
connections — they open their own (cheap on a local file) and read only plain
values, like the stats dataclass, across the thread boundary.
"""

from __future__ import annotations

import fcntl
import os
import threading
from typing import IO, Any, Callable, Protocol

from .config import STATE_DIR, ensure_state_dir


class AlreadyRunning(RuntimeError):
    """Another instance of this server already holds the lock."""


class InstanceLock:
    """An advisory file lock so a server only runs once per machine.

    Two watchdogs against one cursor file interleave rather than duplicate —
    idempotent enqueue sees to that — but they still double the read load and
    make the stats endpoints lie, since each only counts what it happened to
    win. Two supervisors is worse: both hold leases and the pool ping-pongs.
    Easier to refuse the second start than to debug the interleaving.

    flock is released automatically if the process dies, so a hard kill or a
    crash never leaves a stale lock behind.
    """

    def __init__(self, name: str) -> None:
        ensure_state_dir()
        self.path = STATE_DIR / f"{name}.lock"
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        handle = self.path.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                f"another {self.path.stem} is already running (lock: {self.path}). "
                "Stop it first, or set PLUG_ALLOW_MULTIPLE=1 if you really mean to."
            ) from None
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class Loop(Protocol):
    """The shape both server loops share."""

    def run(self, *, handle_signals: bool = True) -> Any: ...

    def request_stop(self, *_: object) -> None: ...


# Returns the loop plus anything needing close() when the thread exits.
Builder = Callable[[], tuple[Loop, list[Any]]]


class BackgroundLoop:
    """Owns a server loop's thread and its startup/shutdown handshake."""

    def __init__(self, name: str, build: Builder, *, single_instance: bool = True) -> None:
        self.name = name
        self._build = build
        # PLUG_ALLOW_MULTIPLE is the deliberate escape hatch for running two
        # copies against separate state directories.
        allow_multiple = os.environ.get("PLUG_ALLOW_MULTIPLE", "").lower() in {"1", "true", "yes"}
        self._lock = InstanceLock(name) if (single_instance and not allow_multiple) else None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.loop: Loop | None = None
        self.finished = False

        # Kept apart on purpose. A build failure means the server never came up,
        # so start() raises and uvicorn refuses to serve. A run failure means it
        # came up and later died, which start() may already have returned from —
        # that one is reported through health() instead. Collapsing the two makes
        # start() raise or not depending on thread timing.
        self.build_error: BaseException | None = None
        self.run_error: BaseException | None = None

    # ---- lifecycle --------------------------------------------------------

    def start(self, *, timeout: float = 30.0) -> None:
        """Start the thread and wait until the loop is built.

        Waiting means a failure that only surfaces on construction — no Full
        Disk Access, no API key — fails uvicorn startup with a real traceback
        instead of leaving a server that answers /health but does nothing.

        Idempotent: mounting an app whose lifespan also starts this loop must
        not spawn a second thread against the same SQLite files.
        """
        if self._thread is not None and self._thread.is_alive():
            return

        # Before spawning anything: refuse to be the second copy.
        if self._lock is not None:
            self._lock.acquire()

        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout):
            self._release_lock()
            raise TimeoutError(f"{self.name} did not start within {timeout}s")
        if self.build_error is not None:
            self._release_lock()
            raise self.build_error

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()

    def _run(self) -> None:
        try:
            loop, closers = self._build()
        except BaseException as exc:  # re-raised by start()
            self.build_error = exc
            self.finished = True
            self._ready.set()
            return

        self.loop = loop
        self._ready.set()
        try:
            # handle_signals=False: uvicorn owns SIGINT/SIGTERM, and signal.signal
            # raises off the main thread anyway.
            loop.run(handle_signals=False)
        except BaseException as exc:  # reported through health()
            self.run_error = exc
        finally:
            self.finished = True
            for closer in closers:
                try:
                    closer.close()
                except Exception:
                    pass

    def stop(self, *, timeout: float = 30.0) -> None:
        try:
            if self.loop is not None:
                self.loop.request_stop()
            if self._thread is not None:
                self._thread.join(timeout)
        finally:
            if self._lock is not None:
                self._lock.release()

    # ---- introspection ----------------------------------------------------

    @property
    def error(self) -> BaseException | None:
        """Whichever failure stopped this loop, if any."""
        return self.build_error or self.run_error

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self.error)

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "finished": self.finished,
            "error": repr(self.error) if self.error else None,
        }
