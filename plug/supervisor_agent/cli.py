"""`plug-supervisor` — the agent server that drains the pool."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import typer
from rich.console import Console

from plug import events, safety as safety_mod
from plug.config import EVENT_LOG, PAUSE_FILE, SPOOL_DB, Config, load_dotenv
from plug.safety import ReplyPolicy
from plug.spool import Spool

from .agent import Agent
from .jobs import JobStore
from .memory import Memory
from .server import SupervisorServer
from .workers import WorkerPool, mcp


app = typer.Typer(help="Supervisor agent: drains the pool and replies.", no_args_is_help=True)

load_dotenv()
console = Console()

OK = "[green]ok[/green]"
FAIL = "[red]fail[/red]"
WARN = "[yellow]warn[/yellow]"


@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run everything except the actual send."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Log to file only, not the terminal."),
) -> None:
    """Start draining the pool and replying."""
    cfg = Config.load(config)

    with Spool() as spool, Memory() as memory, JobStore() as jobs:
        policy = ReplyPolicy(cfg, memory)
        agent = Agent(cfg, memory, policy, dry_run=dry_run, jobs=jobs)
        server = SupervisorServer(
            cfg, spool, memory, policy, agent, echo=not quiet, jobs=jobs
        )
        server.workers = WorkerPool(
            cfg, jobs, open_store=JobStore, client=agent.client, notify=server.notify,
            echo=not quiet,
        )

        mode = (
            "[yellow]DRY RUN — nothing will be sent[/yellow]" if dry_run
            else "[red]LIVE — replies will be sent[/red]"
        )
        depth = spool.stats().depth
        console.print(f"supervisor {server.owner} · {mode}")
        console.print(f"pool ← {SPOOL_DB} (depth {depth}) · model {cfg.model}")
        if cfg.workers.enabled:
            web = "web access" if mcp.wiring(cfg.workers.brightdata) else "[yellow]no web access[/yellow]"
            console.print(
                f"workers: {', '.join(cfg.workers.kinds) or 'none'} "
                f"(max {cfg.workers.max_concurrent} at once) · {web}"
            )

        if safety_mod.is_paused():
            console.print("[yellow]kill switch is ON — sends are blocked until `plug-supervisor resume`[/yellow]")
        console.print("[dim]Ctrl-C to stop; the watchdog keeps pooling meanwhile[/dim]\n")

        stats = server.run()

    console.print(
        f"\nstopped after {stats.ticks} ticks — leased={stats.leased} filtered={stats.filtered} "
        f"batches={stats.batches} sent={stats.sent} skipped={stats.skipped} "
        f"blocked={stats.blocked} retried={stats.retried} "
        f"delegated={stats.delegated} follow_ups={stats.follow_ups}"
    )



@app.command()
def doctor(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """Check everything this server needs. It does not need Full Disk Access."""
    cfg = Config.load(config)
    problems = 0

    try:
        with Spool() as spool:
            s = spool.stats()
            age = f"{s.oldest_pending_age:.0f}s" if s.oldest_pending_age else "-"
            console.print(f"{OK}   pool reachable — pending={s.pending} leased={s.leased} oldest={age}")
            if s.dead:
                console.print(f"{WARN} {s.dead} dead-lettered item(s) — see `dead`")
    except Exception as exc:
        problems += 1
        console.print(f"{FAIL} cannot open pool at {SPOOL_DB}: {exc}")

    proc = subprocess.run(
        ["osascript", "-e", 'tell application "Messages" to count of accounts'],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode == 0:
        console.print(f"{OK}   Messages automation — {proc.stdout.strip()} account(s) visible")
    else:
        problems += 1
        console.print(f"{FAIL} Messages automation denied: {proc.stderr.strip()}")
        console.print("       Approve it in System Settings → Privacy & Security → Automation.")

    if os.environ.get("ANTHROPIC_API_KEY"):
        console.print(f"{OK}   ANTHROPIC_API_KEY present")
    else:
        problems += 1
        console.print(f"{FAIL} ANTHROPIC_API_KEY not set")

    with Memory() as memory:
        console.print(f"{OK}   sends in last hour: {memory.sends_in_last_hour()}")

    if not cfg.workers.enabled:
        console.print(f"{OK}   workers disabled — the agent replies but never delegates")
    else:
        try:
            with JobStore() as jobs:
                js = jobs.stats()
                console.print(
                    f"{OK}   job store reachable — queued={js.queued} running={js.running} "
                    f"ready={js.ready}"
                )
        except Exception as exc:
            problems += 1
            console.print(f"{FAIL} cannot open job store: {exc}")

        wiring = mcp.wiring(cfg.workers.brightdata)
        if wiring:
            console.print(f"{OK}   web research via {mcp.redact(wiring.url)}")
            console.print(f"       tools: {', '.join(wiring.tools)}")
        else:
            # Not fatal. A worker without web access answers from what the model
            # already knows and says so — weaker, not broken.
            console.print(
                f"{WARN} no web research — set {cfg.workers.brightdata.token_env} "
                f"for real lookups"
            )

    if safety_mod.is_paused():

        console.print(f"{WARN} PAUSE file present — sending is disabled ({PAUSE_FILE})")
    else:
        console.print(f"{OK}   not paused")

    console.print(
        f"\n[bold]config[/bold] model={cfg.model} debounce={cfg.supervisor.debounce_seconds}s "
        f"lease={cfg.supervisor.lease_seconds}s "
        f"limits={cfg.safety.per_chat_per_hour}/chat/hr {cfg.safety.global_per_hour}/hr"
    )
    if cfg.chats.only_handles:
        console.print(f"[bold]scoped[/bold] to handles: {', '.join(cfg.chats.only_handles)}")
    raise typer.Exit(code=1 if problems else 0)


@app.command()
def status() -> None:
    """Pool depth, rate-limit usage, and pause state."""
    with Spool() as spool:
        s = spool.stats()
        age = f"{s.oldest_pending_age:.0f}s" if s.oldest_pending_age else "-"
        console.print(
            f"pool:   pending={s.pending} leased={s.leased} done={s.done} "
            f"dropped={s.dropped} dead={s.dead} oldest_pending={age}"
        )
    with Memory() as memory:
        console.print(f"sends last hour: {memory.sends_in_last_hour()}")
    with JobStore() as store:
        j = store.stats()
        console.print(
            f"jobs:   queued={j.queued} running={j.running} ready={j.ready} "
            f"delivered={j.delivered} failed={j.failed} expired={j.expired} blocked={j.blocked}"
        )
    console.print(f"paused:          {safety_mod.is_paused()}")


@app.command()
def jobs(
    chat: Optional[str] = typer.Option(None, "--chat", help="Anonymised chat id, as shown in the log."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Background lookups the agent promised, and what became of them."""
    with JobStore() as store:
        target = None
        if chat:
            target = next(
                (g for g in store.chats() if events.anon(g) == chat or g == chat), None
            )
            if target is None:
                console.print(f"[yellow]no jobs recorded for {chat}[/yellow]")
                raise typer.Exit(code=1)
        rows = store.recent(target, limit=limit)

    if not rows:
        console.print("[dim]no jobs yet[/dim]")
        return
    for job in rows:
        console.print(
            f"{job.job_key}  [bold]{job.state:<9}[/bold] {job.kind:<6} "
            f"chat={events.anon(job.chat_guid)} age={job.age:.0f}s"
        )
        console.print(f"    [dim]{job.objective[:100]}[/dim]")
        if job.reply:
            console.print(f"    → {job.reply[:120]}")
        if job.note:
            console.print(f"    [yellow]{job.note[:120]}[/yellow]")



@app.command()
def dead(
    requeue: bool = typer.Option(False, "--requeue", help="Return dead-lettered items to the pool."),
) -> None:
    """Inspect, and optionally retry, items that failed repeatedly."""
    with Spool() as spool:
        if requeue:
            n = spool.requeue_dead()
            console.print(f"requeued {n} item(s)")
            return
        items = spool.dead_letters()
        if not items:
            console.print("[dim]no dead letters[/dim]")
            return
        for item in items:
            console.print(
                f"[red]#{item.id}[/red] attempts={item.attempts} "
                f"chat={events.anon(item.message.chat_guid)} chars={len(item.message.body)}"
            )


@app.command()
def pause() -> None:
    """Engage the kill switch. Takes effect on the very next send attempt."""
    safety_mod.pause()
    events.emit("safety", "paused")
    console.print(f"[yellow]paused[/yellow] — {PAUSE_FILE}")


@app.command()
def resume() -> None:
    """Release the kill switch."""
    safety_mod.resume()
    events.emit("safety", "resumed")
    console.print("[green]resumed[/green]")


@app.command()
def tail(lines: int = typer.Option(20, "--lines", "-n")) -> None:
    """Tail the shared event log."""
    if not EVENT_LOG.exists():
        console.print("[dim]no events yet[/dim]")
        return
    subprocess.run(["tail", "-n", str(lines), str(EVENT_LOG)])


if __name__ == "__main__":
    app()
