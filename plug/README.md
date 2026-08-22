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

It also listens when nobody is talking to it. In a group chat the agent reads
every burst, remembers what it learns about the people in it, and — when someone
finally pulls it in — can say "hold on, let me look" and dispatch a background
**worker** that researches a real answer on the web and follows up. See
[Workers](#workers).


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
- **A delegated lookup costs two messages.** The holding line and the answer are
  both real sends, both counted against the rate limits.
- **Workers read the live web.** With `BRIGHTDATA_API_TOKEN` set, a worker's
  research runs against Bright Data's MCP server (billed by them; 5,000 requests
  a month are free). Without it, workers still run but answer from what the model
  already knows.
- **It is hackathon-grade.** The HTTP endpoints have no authentication, the queue
  is SQLite rather than a real broker, and the project name is a placeholder.
  Bind to localhost and don't put it on a shared machine.


Guardrails that are on by default: a kill switch, 6 replies per chat per hour
(30 overall), a loop guard so two instances can't ping-pong forever, and filters
that drop verification codes and short-code senders.

## What's included

```
main.py             all three apps in one uvicorn process, and the UI
plug/               shared core
                    config, models, events, eventlog, safety, spool,
                    service (thread + instance lock), notify, mention
watchdog/           server 1 — chat.db → pool
                    db, decode, state, server, cli, main (ASGI)
supervisor_agent/   server 2 — pool → reply
                    agent, send, buffer, memory, server, cli, main (ASGI)
                    planner, personality, dossier   reading the room
                    jobs, llm                       delegated work, model seam
                    workers/  registry, mcp, runner background lookups
                    applescript/  three addressing strategies
console/            server 3 — state → HTTP + SSE. Reads only.
                    server (views), main (ASGI + stream + controls)
web/                the UI — React + TypeScript, Vite, React Flow
                    src/state/pipeline.ts is the glow state machine
tests/              316 tests  (+19 in web/)
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
| `GET /plan` | — | what it has been thinking, per chat |
| `GET /jobs` | — | background lookups and what became of them |

The console adds a third surface at `/api` (and the UI at `/`): `overview`,
`graph`, `artifacts`, `agents`, `log`, `dossier/{chat}`, a `stream` of
server-sent events, and the controls `control/pause`, `control/resume`,
`jobs/{key}/cancel`, `dispatch`.



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
| `test_delegate` | 19 | when a lookup may be dispatched, and every check that stops one |
| `test_dossier` | 19 | fact merging, address coarsening, corrupt-state degradation |
| `test_workers` | 19 | the MCP request shape, retries, timeouts, concurrency cap |
| `test_jobs` | 16 | job lease semantics, quotas, crash recovery |
| `test_follow_up` | 12 | delivering a promised answer, and the one safety exemption |


## Setup

```bash
uv sync
cp .env.example .env      # add your ANTHROPIC_API_KEY
uv run plug doctor        # runs both servers' preflight checks
```

`.env` takes a second, optional key: `BRIGHTDATA_API_TOKEN` gives background
workers real web access. Without it everything still runs — `doctor` says
`warn no web research` and workers answer from model knowledge alone.


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

## Workers

Some questions cannot be answered from a language model's memory. "Where should
we eat?" is one: the answer depends on where these particular people are, when
they can get there, and what is actually open. So the agent can go and look.

```
       plan every burst            addressed?              behind the scenes
   ┌───────────────────────┐   ┌────────────────┐   ┌────────────────────────────┐
   │ facts_learned ─┐      │   │ delegate(      │   │ 1. research ──▶ Bright Data│
   │ action ────────┼──▶   │──▶│   objective,   │──▶│    (MCP, server-side)      │
   │                │      │   │   holding_text)│   │ 2. compose in the group's  │
   │        dossier ◀┘     │   └───────┬────────┘   │    own register            │
   └───────────────────────┘           │            └─────────────┬──────────────┘
                              "hold on, lemme look"               │
                                    sent now                      ▼
                                                    supervisor loop delivers
                                                       the follow-up
```

**The dossier** is what the agent knows about a chat: who is where, when they are
free, what they will not eat, the standing vibe. The planner reports only what
each burst *taught* it; `dossier.py` merges those facts in Python. That split is
deliberate — a plan is a fresh read every time, so letting the model restate the
whole picture would mean an address given on Tuesday disappearing on Wednesday.
Read it with `GET /plan`.

**Delegating** is a third tool alongside `send_reply` and `skip`. It is offered
only when a worker could actually run, and calling it does two things: sends a
short holding message now, and files a job. The reply the model would otherwise
have written is replaced by the promise.

**A worker** is a prompt plus an allowlisted toolset, not a class — the same
shape as the personality pillars. `food` is the only kind so far: a connoisseur
that works the constraints in order (who can reach where, by when, and what they
can't eat) before it works the ratings.

**Research runs through Anthropic's MCP connector**, which holds the connection
to Bright Data's hosted MCP server on Anthropic's side. Nothing MCP-shaped runs
on this machine — no client, no `npx`, no subprocess, no extra terminal. The
toolset is an allowlist (`default_config.enabled: false`, then named tools turned
back on) because that server exposes 60+ tools including browser automation.

Research and voice are separate calls on purpose. The researcher reads scraped
pages and produces notes; the composer never sees a tool and turns notes into one
text message. A poisoned page can therefore make the findings wrong, but it has
nothing to call and no voice to borrow.

### The job lifecycle

`~/.plug/jobs.db` is a leased queue like the pool, for the same reason: the
moment the chat is told to expect an answer, that promise has to survive a crash.

| Outcome | Job state |
|---|---|
| worker found something, follow-up sent | `delivered` |
| worker failed, attempts remain | back to `queued` |
| worker failed for good | `failed`, or `delivered` with an apology line |
| ran past `job_timeout_seconds` | `expired`, or an apology |
| safety refused the follow-up | `blocked` |

```bash
uv run plug-supervisor jobs           # what was promised, and what came of it
curl -s localhost:8002/jobs | python3 -m json.tool
```

### What holds it back

| Guard | Where |
|---|---|
| must have been named in the chat | `plug/mention.py`, re-checked inside the tool |
| one lookup per chat at a time | `JobStore.active_for_chat` |
| `per_chat_cooldown_seconds`, `max_per_chat_per_day` | `JobStore`, not the prompt |
| holding message and follow-up both pass `can_send` | `plug/safety.py` |
| street numbers stripped before anything leaves the machine | `share_locations: coarse` |
| `PLUG_DRY_RUN=1` suppresses both sends | worker still runs, so the chain is checkable |

The follow-up is the one message exempt from the loop guard, and only from that.
The guard exists to stop two automated instances answering each other forever; a
follow-up is the second half of a request a human made, it fires once per job,
and it is already capped by the cooldown and the daily quota. Everything else —
pause, rate limits, length, deny patterns — still applies, which is why a job can
end up `blocked` after the worker succeeded.

**Cost:** two sends per delegation, against 6 per chat per hour. A chat that
delegates three times in an hour is rate-limited.

## The console

`plug tail -f` is the right tool when something is broken and the wrong one for
seeing what this system *is* — the interesting behaviour is invisible by design.
The agent reads every burst and stays quiet; plans pile up unseen; workers
research in the background. All of that is real and none of it is visible in a
chat window.

```bash
cd web && npm install && npm run build     # once
PLUG_DRY_RUN=1 uv run uvicorn main:app --port 8000
open http://localhost:8000
```

One process serves the watchdog, the supervisor, the API and the UI. During
development run `npm run dev` instead and Vite proxies `/api` to port 8000.

**Shared space** is the monitoring view: how much it heard against how rarely it
spoke, and a card per lookup — finished, closed, or merely *considered* (a plan
that named a worker and never became one).

**Backstage** is a pan/zoom canvas of the pipeline that lights up as work moves
through it. The watchdog brightens when it pools a message, the edge travels to
the supervisor, the agent lights when it starts — and when the agent delegates,
**a new worker node appears**, because a thread really was spawned. Nodes fade
back on a timer rather than snapping off, so you can see the shape of a run after
it finishes.

### How it reads state

Events are written twice. `events.jsonl` stays exactly as it was — append-only,
greppable, the thing to read when something is on fire. Alongside it,
`~/.plug/events.db` holds the same records indexed, and its autoincrement `id`
doubles as a stream cursor: the browser asks for `id > n` over SSE, and a laptop
that sleeps mid-run resumes from `Last-Event-ID` instead of losing the run.

Nothing cloud-shaped is in that path on purpose. `emit()` is called from loop
threads, worker threads and error handlers in both servers; a network write there
would sit between the watchdog and its next poll.

```bash
uv run plug backfill      # replay events.jsonl into the index (idempotent)
```

### What it may and may not do

The console is the third server and the only one with no loop. It holds **no
chat.db, no API key, no AppleScript, no model** — the same least-privilege split
that separates the other two, extended to a third member. Its controls are doors
onto switches that already existed: `pause`/`resume` is the same kill switch,
`cancel` settles a job.

The exception is **`POST /api/dispatch`**, which starts a lookup for a chat
nobody tagged. That is the one place the UI loosens the rule `plug/mention.py`
enforces, and it is deliberate: a person pressing a button is a stronger
statement of intent than their name appearing in a message. Everything after it
is unchanged — the follow-up still passes `can_send`, the kill switch and dry
run. Set `console.allow_dispatch: false` to remove it.

**Chat identifiers are anonymised by default**, keyed the same way the event log
keys them, and phone numbers and emails written into the model's own prose are
redacted too — a planner's read of the room routinely contains handles, and this
is the surface most likely to be screenshared. `console.reveal_handles: true`
turns both off.

## Troubleshooting



| Symptom | Cause |
|---|---|
| Agent runs, drafts a reply, nothing arrives | `PLUG_DRY_RUN=1` is set. AppleScript is skipped by design — check `/health`. |
| `permanent_failure` on every message | API account is out of credits. |
| `404` on `POST /notify` | The supervisor is running code from before the endpoint existed. Restart it. |
| `AlreadyRunning` on startup | Another instance holds the lock. `pgrep -fl uvicorn`. The lock is `flock`-based and can never go stale, so if nothing is running that's a bug. |
| Config edits ignored | Config is read at import. Restart, and check `config_source` in `/status`. |
| Nothing is pooled at all | Check Full Disk Access, then `uv run plug-watchdog doctor`. |
| Replies stop after a while | Rate limit (6/chat/hour). Remember a delegation costs two. Check `plug status`. |
| It never delegates | `workers.enabled`, or the chat is inside its cooldown / daily quota. `plug-supervisor jobs` shows the last one. |
| Recommendations are vague or dated | No `BRIGHTDATA_API_TOKEN`, so the worker had no web access. `doctor` says so. |
| A job sits in `ready` | The follow-up is being refused — paused, or rate-limited. The `note` on the job says which. |
| A job sits in `running` forever | It is retired after `job_timeout_seconds` on the next tick. |
| The console shows no history | `events.db` starts empty. `uv run plug backfill` replays the JSONL into it. |
| The UI 404s at `/` | No `web/dist`. `cd web && npm run build`, or use the Vite dev server. |
| The stream indicator says `down` | The API is unreachable, or a proxy is buffering `text/event-stream`. |



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

## Group chats: read always, speak rarely

In a group chat the agent behaves differently from a 1:1. It reads every burst
and stays silent unless someone says its name.

**The planning pass.** On each burst it runs a separate model call that produces
a structured plan — what's happening, the room's mood, open threads, what it
*would* say, and which register it would use — and then does nothing with it.
Same shape as plan mode: form an intent, write it down, don't act. The plan is
stored per chat and fed to the next planning pass, so its read is continuous
rather than reconstructed after the fact.

Watch it think without it ever speaking:

```bash
curl -s localhost:8002/plan | python3 -m json.tool     # every chat it's following
curl -s "localhost:8002/plan?chat=<hash>&limit=5"      # how one read evolved
```

**The four pillars.** The planner picks which register to lead with based on the
tone it just read — one voice with four settings, not four bots.

| Pillar | Register |
|---|---|
| `hype` | Extremely enthusiastic. Genuinely excited, amplifies other people. |
| `flow` | Extremely laid-back. Goes with it, never escalates. |
| `drama` | Theatrical. Everything is a Moment, stakes inflated for comedy. |
| `deadpan` | Dry and understated. The joke lands because it isn't announced. |

Observed selections: someone's good news → `hype`; a bereavement → `flow`;
logistics → `flow`; petty bickering → `drama`.

**Tagging is text-based, and it is authoritative.** The agent speaks when its
name appears on a word boundary — `plug`, `@plug`, `Plug,` — but not inside
`plugin`, `unplugged`, or `plug-in`. iMessage's own `has_unseen_mention` column
is not usable for this: it's an *unseen* flag, cleared the moment you read the
message, and only 9 rows in 156,000 ever had it set.

The planner also reports its own `addressed_to_us`, but that is advisory only.
The model does not get to decide it was invited — that would be asking a model
to gate an action it wants to take. The detector decides, the send tool
re-checks, and there is a test asserting a message that *claims* to tag the
agent still results in silence.

**Config** (`plug.yml`):

```yaml
group:
  agent_name: plug
  aliases: []
  respond_when_tagged_only: true   # turning this off puts an LLM into a
                                   # friend group's chat uninvited
  plan_every_burst: true           # one model call per burst, even when silent
  answer_direct_questions: false   # treat "what do you think?" as a tag when
                                   # we spoke recently — a guess, so off
```

**The loop guard is split** for groups. At intake it does not apply, or the agent
would be blind to the conversation for 30 seconds after every reply — exactly
the messages it needs to stay oriented. It applies at send time instead.

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

260 tests. Database-backed tests skip automatically without Full Disk Access.
The worker tests stub the model and never reach the network, so they run
identically with or without an API key.

