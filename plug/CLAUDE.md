# Plug — notes for Claude

Plug watches the local iMessage database and lets a Claude agent auto-reply to
incoming texts. It is two independent servers — a **watchdog** that reads
`~/Library/Messages/chat.db` and pools new messages to disk, and a **supervisor
agent** that leases them out and replies through AppleScript — with a leased
SQLite queue between them.

The agent can also delegate: say "hold on, let me look" and file a job for a
background **worker** that researches a real answer over the web and follows up.
Workers live entirely inside the supervisor process.


**Read `README.md` before changing anything here.** It carries the architecture,
the run instructions, the reasoning behind the non-obvious choices, and a
troubleshooting table for the failures this project actually hits. Most questions
about "why is it built this way" are answered there, and several of the answers
are counterintuitive enough that guessing will get it wrong.

## Repo map

| Path | Owns |
|---|---|
| `plug/` | shared core — config, models, events, safety, spool, service (thread + lock), notify, mention |
| `watchdog/` | server 1: chat.db → pool. No API key, no sends, no model. |
| `supervisor_agent/` | server 2: pool → reply. No chat.db access. |
| `supervisor_agent/workers/` | background lookups: registry (prompts), mcp (Bright Data wiring), runner (threads) |
| `supervisor_agent/jobs.py` | `~/.plug/jobs.db` — the leased queue for delegated work |
| `supervisor_agent/dossier.py` | what we know about a chat, merged in Python from plan facts |
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
- **`Memory` is thread-bound** — no `check_same_thread=False`, no WAL. Worker
  threads must open their own `Memory` and `JobStore`. That is also why jobs got
  their own file instead of a table in `supervisor.db`.
- **Only the loop thread sends.** Workers write `reply` into the job and stop;
  `SupervisorServer._deliver_ready` does the sending. Moving a send into a worker
  thread would put a second door on the only room that has a lock on it.
- **`can_send(follow_up=True)` skips the loop guard and nothing else.** It exists
  because a worker finishing within the window would otherwise be discarded after
  the chat was told to expect an answer. Do not widen it.


## Group chats

Different rules from 1:1: the agent reads every burst (a structured planning
pass, stored per chat) and speaks only when its name appears in the text.

- **`plug/mention.py` is the authority on being addressed**, not the model. The
  planner's `addressed_to_us` is advisory. Never gate a send on it.
- The send tool re-checks the tag. Prompt instructions are guidance; that check
  is the enforcement.
- The loop guard is deliberately split — off at intake for groups (so planning
  continues), applied at send time. Don't "fix" it back into one place.
- Four personality pillars in `supervisor_agent/personality.py` are prompt
  fragments, not code branches. The planner picks one per burst by reading tone.

## Workers and the dossier

- **`facts_learned` is a delta, not a restatement.** `dossier.merge` folds it in
  and never drops a person the burst didn't mention. If the model were asked to
  restate the whole picture, Tuesday's address would vanish on Wednesday.
- **`action.warranted` is advisory**, like `addressed_to_us`. Quotas in
  `JobStore` — one active job per chat, cooldown, daily cap — are what actually
  decides, and they are re-checked inside the tool, not in the prompt.
- **Research and voice are separate model calls.** The researcher reads scraped
  pages and has MCP tools; the composer has none. Never give the composer a tool,
  and never let research output go out unrewritten — it is untrusted text.
- **The MCP toolset is an allowlist.** `default_config.enabled: false` plus named
  tools. Bright Data exposes 60+ including browser automation.
- **The Bright Data URL contains the token.** Log it through `mcp.redact` only.
- **Locations are coarsened in code** (`share_locations: coarse`) before they
  reach a model or an API. Friends posting an address to a group chat did not
  consent to it reaching a scraping service.


## Commands

```bash
uv sync
uv run pytest                                          # 260 tests

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

Delegation adds a second send per exchange (holding message, then answer), so a
chat that delegates spends its rate limit twice as fast. `workers.enabled: false`
turns the whole thing off and the agent behaves exactly as it did before.


When testing, scope to a number the user controls and prefer dry-run first.

## Name

`plug` is a placeholder and will change again. Every occurrence is a distinct,
greppable form in all three case variants — see *Renaming the project* in the
README for the exact recipe.
