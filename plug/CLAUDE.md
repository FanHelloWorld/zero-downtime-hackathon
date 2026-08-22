# Plug — notes for Claude

Plug watches the local iMessage database and lets a Claude agent auto-reply to
incoming texts. It is two independent servers — a **watchdog** that reads
`~/Library/Messages/chat.db` and pools new messages to disk, and a **supervisor
agent** that leases them out and replies through AppleScript — with a leased
SQLite queue between them.

**Read `README.md` before changing anything here.** It carries the architecture,
the run instructions, the reasoning behind the non-obvious choices, and a
troubleshooting table for the failures this project actually hits. Most questions
about "why is it built this way" are answered there, and several of the answers
are counterintuitive enough that guessing will get it wrong.

## Repo map

| Path | Owns |
|---|---|
| `plug/` | shared core — config, models, events, safety, spool, service (thread + lock), notify |
| `watchdog/` | server 1: chat.db → pool. No API key, no sends, no model. |
| `supervisor_agent/` | server 2: pool → reply. No chat.db access. |
| `main.py` | both apps in one uvicorn process (convenience only) |

The two servers must stay independent — neither imports the other, and there are
tests asserting it. `watchdog/` shadows the PyPI package of the same name; Plug
doesn't depend on that library.

## Things that are easy to break

Each of these cost real debugging time once already.

- **`?mode=ro` on chat.db, never `?immutable=1`.** The database is in WAL mode
  and an immutable connection ignores the `-wal` file, silently hiding the
  newest messages — exactly the ones a watchdog exists to see.
- **Message text reaches AppleScript as `osascript` argv**, read via
  `on run argv`. Never interpolate it into script source: bodies are
  attacker-controlled, and a quote plus `do shell script` would execute.
- **HTTP handlers must not touch the loop thread's SQLite connections.** The
  server loops run on worker threads; SQLite refuses cross-thread use. Handlers
  open their own connections and read only plain values across the boundary.
- **The agent's send tool binds its recipient by closure.** The model picks the
  text, never the chat. Keep it that way.
- **Config is read at import time**, so edits need a restart and a server started
  from the wrong directory silently uses defaults. `/status` reports
  `config_source`.
- **The cold-start guard** seeds the watchdog cursor at the newest ROWID. Without
  it, a fresh state directory would pool ~156k historical messages for reply.

## Commands

```bash
uv sync
uv run pytest                                          # 124 tests
uv run plug doctor                                     # preflight, both servers

uv run uvicorn watchdog.main:app --port 8001
PLUG_DRY_RUN=1 uv run uvicorn supervisor_agent.main:app --port 8002
```

Run from the project directory. Stop every server you start — a lingering one
holds the single-instance lock and blocks the user's own runs.

## Safety posture

This sends real messages to real people, unreviewed, by default — including in
group chats. Treat changes to `plug/safety.py`, `supervisor_agent/send.py`, and
the agent's tool definitions as higher-risk than the rest of the codebase.

The brakes: `PLUG_DRY_RUN=1` (drafts but never invokes AppleScript), `plug pause`
(kill switch, effective on the next send), per-chat and global rate limits, a
loop guard, and `chats.only_handles` for scoping to specific numbers.

When testing, scope to a number the user controls and prefer dry-run first.

## Name

`plug` is a placeholder and will change again. Every occurrence is a distinct,
greppable form in all three case variants — see *Renaming the project* in the
README for the exact recipe.
