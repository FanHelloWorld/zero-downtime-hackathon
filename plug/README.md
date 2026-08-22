# Plug

> `plug` is a placeholder name. See [Renaming the project](#renaming-the-project).

## What this is

Plug watches your Mac's iMessage database and lets a Claude agent reply to
incoming texts on your behalf, automatically.

It is two independent servers with a durable disk pool between them. The
**watchdog** does nothing but read Apple's message database on a timer and write
new messages into the pool. The **supervisor agent** leases messages out of the
pool, decides what to say, and sends the reply by driving Messages.app through
AppleScript.

```
                    ┌──────────────────────┐
   ~/Library/       │  watchdog  :8001     │
   Messages/  ──────▶  polls chat.db /3s   │──┐
   chat.db          │  no model, no sends  │  │
                    └───────────┬──────────┘  │  pool (SQLite, leased queue)
                                │             ├──▶  ~/.plug/spool.db
                       POST /notify           │
                                │             │
                    ┌───────────▼──────────┐  │
   Messages.app ◀───│ supervisor agent     │◀─┘
   (AppleScript)    │ :8002                │
                    │ leases, replies, acks│
                    └──────────────────────┘
```

The two processes share nothing but that pool file. Neither imports the other —
there is a test that asserts it.

## What to expect

Read this before you run it. Some of these will surprise you otherwise.

- **It sends real messages to real people.** By default it auto-replies to
  everyone who texts you, in 1:1 *and* group chats, with no review step. People
  who never opted in will receive text written by a language model.
- **macOS only**, and it needs two separate permissions: Full Disk Access (to
  read the message database) and Automation → Messages (to send).
- **You need an Anthropic API key with credits on it.** An empty balance is the
  single most common failure — it looks like "the agent does nothing", and shows
  up in the log as `permanent_failure`.
- **Replies are not instant.** Expect roughly 7 seconds: up to 3s for the
  watchdog's poll, then a 4s debounce so a burst of texts gets one reply instead
  of four. Both are configurable.
- **Dry-run mode looks like a bug if you forget it's on.** With `PLUG_DRY_RUN=1`
  the agent runs and drafts a reply, then deliberately stops short of
  AppleScript. Check `dry_run` in `/health`.
- **It is hackathon-grade.** The HTTP endpoints have no authentication, the queue
  is SQLite rather than a real broker, and the project name is a placeholder.
  Bind to localhost and don't put it on a shared machine.

Guardrails that are on by default: a kill switch, 6 replies per chat per hour
(30 overall), a loop guard so two instances can't ping-pong forever, and filters
that drop verification codes and short-code senders.

## What's included

```
main.py             both apps in one uvicorn process (convenience only)
plug/               shared core
                    config, models, events, safety, spool,
                    service (thread + instance lock), notify
watchdog/           server 1 — chat.db → pool
                    db, decode, state, server, cli, main (ASGI)
supervisor_agent/   server 2 — pool → reply
                    agent, send, buffer, memory, server, cli, main (ASGI)
                    applescript/  three addressing strategies
tests/              124 tests
```

Note: the local `watchdog/` package shadows the PyPI package of the same name.
Plug does not depend on that library; if you add a dependency that does, rename
this package.

**Three CLIs** (`plug`, `plug-watchdog`, `plug-supervisor`) and **two ASGI apps**
for uvicorn. Endpoints:

| | watchdog :8001 | supervisor :8002 |
|---|---|---|
| `GET /health` | 503 once the loop dies | plus `paused`, `dry_run` |
| `GET /status` | cursor, stats, pool, `config_source` | owner, stats, pool, sends/hour |
| `GET /metrics` | Prometheus text | Prometheus text |
| `POST /tick` | force an immediate poll | — |
| `POST /notify` | — | wake the drain loop (the watchdog calls this) |
| `POST /pause` `/resume` | — | kill switch |
| `GET /dead` `POST /dead/requeue` | — | dead-letter queue |

**The test suite**, by area:

| Area | Tests | Covers |
|---|---:|---|
| `test_safety` | 23 | every guard in isolation: kill switch, rate limits, loop guard, OTP filters |
| `test_supervisor_server` | 17 | drain loop, ack/drop/retry, dry-run vs live AppleScript |
| `test_spool` | 15 | lease semantics, crash recovery, dead-lettering, idempotent enqueue |
| `test_service` | 12 | background thread lifecycle, single-instance lock |
| `test_http` | 12 | both ASGI surfaces, including a cross-thread SQLite regression |
| `test_notify` | 9 | the watchdog→agent alert, over real HTTP |
| `test_buffer` | 9 | burst coalescing |
| `test_watchdog_server` | 8 | poll loop, cursor advance, duplicate suppression |
| `test_db` / `test_decode` | 9 | live chat.db reads and the `attributedBody` decoder |
| `test_memory` / `test_watchdog_state` | 7 | history, send log, cold-start guard |
| `test_send_injection` | 3 | AppleScript injection safety, via real `osascript` |

## Setup

```bash
uv sync
cp .env.example .env      # add your ANTHROPIC_API_KEY
uv run plug doctor        # runs both servers' preflight checks
```

`doctor` verifies the whole chain: chat.db readability, WAL freshness, the
`attributedBody` decoder, Messages automation permission, the API key, the
cursor, and the pause state.

Two macOS permissions, one per server:

| Permission | Where | Needed by |
|---|---|---|
| Full Disk Access | Privacy & Security → Full Disk Access | watchdog (reading `chat.db`) |
| Automation → Messages | Privacy & Security → Automation | supervisor (sending) |

## How to run

### 1. Start safe

Two terminals. Run both **from the project directory** — config is read at
import, so starting elsewhere silently falls back to defaults.

```bash
# terminal 1 — watchdog. Never sends anything; safe to leave running.
cd "/path/to/project"
uv run uvicorn watchdog.main:app --port 8001
```

```bash
# terminal 2 — supervisor, drafting but not sending
cd "/path/to/project"
PLUG_DRY_RUN=1 uv run uvicorn supervisor_agent.main:app --port 8002
```

Confirm what you're running:

```bash
curl -s localhost:8002/health     # expect "dry_run":true
curl -s localhost:8001/status | python3 -m json.tool | grep config_source
```

### 2. Watch it work

```bash
uv run plug tail -f    # live event log from both servers
```

Text yourself, and you should see the chain:

```
watchdog    spooled      {'count': 1}
supervisor  dispatch     {'size': 1}
agent       start
send        dry_run      {'would_send': '...', 'note': 'AppleScript NOT invoked'}
```

### 3. Scope it before going live

In `plug.yml`, restrict it to a number you control:

```yaml
chats:
  only_handles: ["+15551234567"]
```

### 4. Go live

Drop `PLUG_DRY_RUN=1`:

```bash
uv run uvicorn supervisor_agent.main:app --port 8002
```

The `send` event should now read `delivered {'strategy': 'send_to_chat'}`. Widen
by emptying `only_handles` — that means everyone, including group chats.

### Controls

```bash
curl -X POST localhost:8002/pause     # kill switch, effective on the next send
curl -X POST localhost:8002/resume
uv run plug pause                     # same, from the CLI
uv run plug status                    # pool depth and pause state
curl -s localhost:8002/dead           # messages that failed repeatedly
```

Stop with Ctrl-C in each terminal, or:

```bash
pkill -f "uvicorn watchdog"; pkill -f "uvicorn supervisor"
```

### Environment

uvicorn takes no app flags, so these are the knobs:

| Variable | Meaning |
|---|---|
| `PLUG_CONFIG` | config path (default `plug.yml`, resolved at **import** time) |
| `PLUG_DRY_RUN` | `1` to draft replies without sending |
| `PLUG_ALLOW_MULTIPLE` | `1` to bypass the single-instance lock |

Each server takes a machine-wide lock (`~/.plug/<name>.lock`) and refuses to
start twice. Two watchdogs interleave rather than duplicate — idempotent enqueue
sees to that — but they double the read load and make each one's stats
undercount.

### Without uvicorn

```bash
uv run plug-watchdog serve            # terminal 1
uv run plug-supervisor serve          # terminal 2  (add --dry-run first)
uv run plug up --dry-run              # or both at once, for demos
```

The CLI has no HTTP surface, so set `watchdog.notify_url: null` in `plug.yml`
when using it — the supervisor falls back to polling.

For a real deployment run each under its own supervisor (launchd, systemd, tmux)
so one can restart without the other. `main.py` at the repo root mounts both
under one uvicorn process for single-container deploys; it works, but it gives
up the isolation the split exists for.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Agent runs, drafts a reply, nothing arrives | `PLUG_DRY_RUN=1` is set. AppleScript is skipped by design — check `/health`. |
| `permanent_failure` on every message | API account is out of credits. |
| `404` on `POST /notify` | The supervisor is running code from before the endpoint existed. Restart it. |
| `AlreadyRunning` on startup | Another instance holds the lock. `pgrep -fl uvicorn`. The lock is `flock`-based and can never go stale, so if nothing is running that's a bug. |
| Config edits ignored | Config is read at import. Restart, and check `config_source` in `/status`. |
| Nothing is pooled at all | Check Full Disk Access, then `uv run plug-watchdog doctor`. |
| Replies stop after a while | Rate limit (6/chat/hour). Check `plug status`. |

## Why it's split

| | watchdog | supervisor agent |
|---|---|---|
| Reads `chat.db` | yes | **no** |
| Needs Full Disk Access | yes | **no** |
| Needs Automation permission | no | yes |
| Needs `ANTHROPIC_API_KEY` | **no** | yes |
| Calls a model | never | yes |
| Sends messages | never | yes |

That split buys three things:

- **Capture survives the agent.** Stop, upgrade, or crash the supervisor and the
  watchdog keeps pooling. Messages received meanwhile are replied to on catch-up
  rather than lost.
- **Least privilege.** The component holding Full Disk Access is small, has no
  API key, and never executes AppleScript. The component that talks to the
  network and to Messages.app cannot read your message history.
- **Policy reloads cheaply.** Rate limits, deny patterns, and the kill switch all
  live supervisor-side, so changing them means restarting the supervisor alone.

## How the watchdog alerts the agent

The watchdog `POST`s to the supervisor's `/notify` the moment it pools anything,
so work is picked up immediately instead of waiting out `idle_sleep_seconds`.

The ping is deliberately weak: fire-and-forget on its own thread, carrying no
message data, coalesced while one is in flight, and with every failure swallowed
and throttled in the log. The supervisor **also polls**, so the alert is a
latency optimisation and never a dependency — a supervisor that is stopped,
restarted, moved, or run from the CLI just picks the work up on its next poll.
That is what keeps the two servers independently restartable.

Measured end to end: pooled at `17:33:56.990`, dispatched at `17:34:01.051` —
the 4s gap is the configured debounce, not the handoff.

## The pool

`~/.plug/spool.db` is a leased SQLite queue, not a plain log:

- **Idempotent enqueue.** Keyed on the chat.db ROWID, so a watchdog restart that
  re-reads a window cannot produce a duplicate reply.
- **Leases, not deletes.** The supervisor claims items with an expiry. If it dies
  mid-reply the lease lapses and the work is retaken rather than lost.
- **Terminal states are auditable.** `done`, `dropped` (filtered or skipped), and
  `dead` (failed repeatedly) all persist until `purge`.
- **Backpressure is visible.** `plug status` shows depth and the age of the oldest
  pending item, so a stalled agent is obvious instead of silent.

Failure handling:

| Outcome | Pool result |
|---|---|
| replied | `done` |
| agent skipped, or policy filtered | `dropped` |
| safety blocked (rate limit, kill switch) | `dropped` — a reply an hour late is worse than none |
| transient failure (429, 5xx, send failure) | requeued, up to `max_attempts` |
| permanent failure (400, auth, billing) | `dropped` immediately — retrying cannot help |
| retried past `max_attempts` | `dead`, inspect with `plug-supervisor dead` |

## Implementation notes

### Reading messages

The connection uses `?mode=ro`, never `?immutable=1`. chat.db runs in WAL mode,
and an immutable connection ignores the `-wal` file — measured here, it reported
max ROWID 176617 while the true value was 176627. The ten messages it hides are
exactly the newest ones, which is the entire point of a watchdog.

Recent macOS versions often leave `message.text` NULL and store the body only in
`message.attributedBody`, a serialized `NSAttributedString` in Apple's binary
typedstream format. `watchdog/decode.py` extracts it. The tests validate this
against your live database: rows carrying *both* columns must decode to exactly
the plain-text value.

Tapbacks (`associated_message_type` 2000–2006) and system rows (`item_type != 0`)
are filtered out, so the agent doesn't reply to a thumbs-up.

### Cold start

On first run the watchdog seeds its cursor at the current maximum ROWID. Without
that guard, a fresh install would pool ~156,000 historical messages for reply.

### Debouncing

Bursts are coalesced supervisor-side, not in the watchdog: the watchdog's job is
to get messages onto disk quickly, and holding them in memory to wait out a burst
would put unpooled data at risk. Four rapid messages become one reply.

### Sending

Message bodies are attacker-controlled text that ends up in an AppleScript
program. They are passed as `osascript` **arguments** and read via `on run argv`,
never interpolated into script source — otherwise a message containing a quote
followed by `do shell script` would run arbitrary commands. A test fires a
hostile payload through `osascript` and asserts it comes back inert.

On macOS 26, AppleScript's `chat id` values match `chat.guid` from the database
verbatim, so addressing a chat by guid works directly. Participant and buddy
addressing remain as ordered fallbacks.

### Agent

The agent gets exactly two tools: `send_reply` and `skip`. The recipient is bound
by a closure, **not** chosen by the model — whatever it decides, it can only
address the chat the batch came from. `ReplyPolicy.can_send()` runs inside the
send tool before delivery, so a block returns to the model as a normal tool
result rather than raising.

### Threading under uvicorn

Each server's loop is synchronous — it sleeps and talks to SQLite — so it runs on
a worker thread started by the ASGI lifespan while the app keeps serving HTTP.
SQLite connections belong to the thread that opened them, so **HTTP handlers must
never touch the loop's connections**; they open their own (cheap on a local file)
and read only plain values, like the stats dataclass, across the boundary.

## Renaming the project

`plug` is a placeholder. To swap it for the real name, stop all servers, then:

```bash
# 1. the shared core package
mv plug <newname>

# 2. every occurrence, all three case forms
grep -rlI --exclude-dir=.venv --exclude-dir=.git --exclude-dir=__pycache__ \
  -e PLUG -e Plug -e plug . \
| xargs sed -i '' -e 's/PLUG/<NEWNAME>/g' -e 's/Plug/<Newname>/g' -e 's/plug/<newname>/g'

# 3. the config file
mv plug.yml <newname>.yml

# 4. re-register the console scripts
uv sync && uv run pytest
```

That also moves the state directory to `~/.<newname>/`. The old one is left in
place; the cold-start guard means a fresh state directory seeds its cursor at the
newest message rather than replying to your history.

## Testing

```bash
uv run pytest
```

124 tests. Database-backed tests skip automatically without Full Disk Access.
