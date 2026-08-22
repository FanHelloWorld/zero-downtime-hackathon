"""`plug` — cross-cutting commands spanning both servers.

The two servers are independent and each has its own CLI (`plug-watchdog`,
`plug-supervisor`). This umbrella exists for the things that span them: a
combined preflight, combined status, the shared kill switch, and a convenience
launcher for running both in one terminal.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Optional

import typer
from rich.console import Console

from . import events, safety as safety_mod
from .config import EVENT_LOG, SPOOL_DB, Config, load_dotenv
from .spool import Spool

app = typer.Typer(
    help="iMessage watchdog + supervisor agent. Two independent servers, one disk pool.",
    no_args_is_help=True,
)

load_dotenv()
console = Console()


@app.command()
def doctor(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """Run both servers' preflight checks."""
    args = ["--config", config] if config else []
    failures = 0

    console.print("[bold]watchdog[/bold]")
    failures += subprocess.run([sys.executable, "-m", "watchdog", "doctor", *args]).returncode

    console.print("\n[bold]supervisor agent[/bold]")
    failures += subprocess.run([sys.executable, "-m", "supervisor_agent", "doctor", *args]).returncode

    raise typer.Exit(code=1 if failures else 0)


@app.command()
def status() -> None:
    """Pool depth and kill-switch state at a glance."""
    with Spool() as spool:
        s = spool.stats()
        age = f"{s.oldest_pending_age:.0f}s" if s.oldest_pending_age else "-"
        console.print(f"pool:   {SPOOL_DB}")
        console.print(
            f"        pending={s.pending} leased={s.leased} done={s.done} "
            f"dropped={s.dropped} dead={s.dead} oldest_pending={age}"
        )
        if s.pending and s.oldest_pending_age and s.oldest_pending_age > 60:
            console.print("[yellow]        backlog is aging — is the supervisor running?[/yellow]")
    console.print(f"paused: {safety_mod.is_paused()}")


@app.command()
def up(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Supervisor runs without sending."),
) -> None:
    """Launch both servers in this terminal, for demos and local runs.

    They remain separate processes with separate lifecycles — this only saves
    opening two terminals. In a real deployment run each under its own
    supervisor (launchd, systemd, tmux) so one can restart without the other.
    """
    cfg_args = ["--config", config] if config else []
    procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(name: str, module: str, extra: list[str]) -> None:
        proc = subprocess.Popen([sys.executable, "-m", module, "serve", *cfg_args, *extra])
        procs.append((name, proc))

    console.print(f"[dim]pool {SPOOL_DB} · events {EVENT_LOG}[/dim]")
    spawn("watchdog", "watchdog", [])
    # Let the watchdog seed its cursor before the supervisor starts leasing.
    time.sleep(1.0)
    spawn("supervisor", "supervisor_agent", ["--dry-run"] if dry_run else [])

    def shutdown(*_: object) -> None:
        for name, proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while any(proc.poll() is None for _, proc in procs):
            time.sleep(0.25)
    finally:
        shutdown()
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    for name, proc in procs:
        console.print(f"{name} exited with {proc.returncode}")


@app.command()
def pause() -> None:
    """Engage the kill switch. Blocks sends immediately, across every supervisor."""
    safety_mod.pause()
    events.emit("safety", "paused")
    console.print("[yellow]paused[/yellow] — sends are blocked; the watchdog keeps pooling")


@app.command()
def resume() -> None:
    """Release the kill switch."""
    safety_mod.resume()
    events.emit("safety", "resumed")
    console.print("[green]resumed[/green]")


@app.command()
def tail(
    lines: int = typer.Option(30, "--lines", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f"),
) -> None:
    """Tail the shared event log — both servers write to it."""
    if not EVENT_LOG.exists():
        console.print("[dim]no events yet[/dim]")
        return
    cmd = ["tail", "-n", str(lines)] + (["-f"] if follow else []) + [str(EVENT_LOG)]
    subprocess.run(cmd)


@app.command()
def purge(
    days: float = typer.Option(7.0, "--days", help="Delete settled pool rows older than this."),
) -> None:
    """Housekeeping on the pool. The watchdog also does this hourly on its own."""
    with Spool() as spool:
        removed = spool.purge(days * 24 * 3600)
    console.print(f"removed {removed} settled row(s)")

    # Settled jobs age out on the same schedule. Imported here rather than at
    # module level: `plug` is the shared CLI, and the job store is supervisor
    # state — a top-level import would put it in the watchdog's path too.
    from supervisor_agent.jobs import JobStore

    with JobStore() as jobs:
        removed_jobs = jobs.purge(days * 24 * 3600)
    console.print(f"removed {removed_jobs} settled job(s)")



if __name__ == "__main__":
    app()
