"""`plug-watchdog` — the disk-polling server."""

from __future__ import annotations

import subprocess
from typing import Optional

import typer
from rich.console import Console

from plug.config import CHAT_DB, EVENT_LOG, SPOOL_DB, Config
from plug.spool import Spool

from .db import ChatDB
from .decode import decode_attributed_body
from .server import WatchdogServer
from .state import WatchdogState

app = typer.Typer(help="Watchdog server: polls chat.db and pools messages to disk.", no_args_is_help=True)
console = Console()

OK = "[green]ok[/green]"
FAIL = "[red]fail[/red]"
WARN = "[yellow]warn[/yellow]"


@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Log to file only, not the terminal."),
    once: bool = typer.Option(False, "--once", help="Run a single poll and exit."),
) -> None:
    """Start polling. Runs indefinitely; safe to restart at any time."""
    cfg = Config.load(config)

    with ChatDB() as db, WatchdogState() as state, Spool() as spool:
        seeded = state.seed_cursor(db.max_rowid())
        server = WatchdogServer(cfg, db, state, spool, echo=not quiet)

        console.print(
            f"watchdog polling every {cfg.watchdog.poll_interval_seconds}s "
            f"from ROWID {seeded}"
        )
        console.print(f"pool → {SPOOL_DB}")
        console.print("[dim]no model calls, no sends — this server only reads and pools[/dim]\n")

        if once:
            spooled = server.tick()
            console.print(f"one tick: spooled {spooled}")
            return

        stats = server.run()

    console.print(
        f"\nstopped after {stats.ticks} ticks — seen={stats.seen} spooled={stats.spooled} "
        f"filtered={stats.filtered} duplicates={stats.duplicates}"
    )


@app.command()
def doctor(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """Check everything this server needs. Notably, it does not need an API key."""
    cfg = Config.load(config)
    problems = 0

    try:
        with ChatDB() as db:
            mode = db.journal_mode()
            top = db.max_rowid()
            console.print(f"{OK}   chat.db readable — journal_mode={mode}, max ROWID={top}")
            if mode.lower() != "wal":
                console.print(f"{WARN} expected WAL mode; cursor freshness assumptions may differ")

            samples = db.decode_samples(200)
            matches = sum(1 for text, blob in samples if decode_attributed_body(blob) == text)
            if samples and matches == len(samples):
                console.print(f"{OK}   attributedBody decoder — {matches}/{len(samples)} exact")
            elif samples:
                problems += 1
                console.print(f"{FAIL} attributedBody decoder — only {matches}/{len(samples)} exact")
            else:
                console.print(f"{WARN} no rows with both text and attributedBody to test against")
    except Exception as exc:
        problems += 1
        console.print(f"{FAIL} cannot read {CHAT_DB}: {exc}")
        console.print("       Grant Full Disk Access to your terminal in System Settings → Privacy & Security.")

    with WatchdogState() as state:
        cursor = state.get_cursor()
        if cursor is None:
            console.print(f"{WARN} no cursor yet; first run seeds it at the current newest message")
        else:
            console.print(f"{OK}   cursor at ROWID {cursor}")

    try:
        with Spool() as spool:
            s = spool.stats()
            console.print(f"{OK}   pool writable — depth={s.depth} (pending={s.pending}, leased={s.leased})")
    except Exception as exc:
        problems += 1
        console.print(f"{FAIL} cannot open pool at {SPOOL_DB}: {exc}")

    console.print(
        f"\n[bold]config[/bold] poll={cfg.watchdog.poll_interval_seconds}s "
        f"read_limit={cfg.watchdog.read_limit} groups={cfg.chats.include_groups} "
        f"services={','.join(cfg.chats.services)}"
    )
    console.print(f"events → {EVENT_LOG}")
    raise typer.Exit(code=1 if problems else 0)


@app.command()
def status() -> None:
    """Cursor position and pool depth."""
    with WatchdogState() as state:
        console.print(f"cursor: {state.get_cursor()}")
    with Spool() as spool:
        s = spool.stats()
        age = f"{s.oldest_pending_age:.0f}s" if s.oldest_pending_age else "-"
        console.print(
            f"pool:   pending={s.pending} leased={s.leased} done={s.done} "
            f"dropped={s.dropped} dead={s.dead} oldest_pending={age}"
        )


@app.command()
def tail(lines: int = typer.Option(20, "--lines", "-n")) -> None:
    """Tail the shared event log."""
    if not EVENT_LOG.exists():
        console.print("[dim]no events yet[/dim]")
        return
    subprocess.run(["tail", "-n", str(lines), str(EVENT_LOG)])


if __name__ == "__main__":
    app()
