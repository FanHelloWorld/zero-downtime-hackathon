# Zero Downtime Hackathon

This repository contains the prototype for **Sediment**: a quiet-agent workspace that lets background agents complete useful work and leave results in a shared space instead of interrupting a conversation. The root UI is a self-contained concept/demo; the `plug/` package is the working macOS message-ingestion and reply service behind the operational prototype.

## What is here

### Sediment UI

[`sediment-ui-en.html`](sediment-ui-en.html) is a dependency-free browser demo. It presents:

- **Shared space**: finished artifacts, provenance, agent status, filters, and a 72-hour activity meter.
- **Backstage**: the agent-builder/workflow view, including running, always-on, retired, and pending agents.
- A small interactive demo: tabs, filters, artifact actions, and the `N` shortcut to land a newly finished item.

The UI currently uses in-file sample state and does not call the backend.

### Plug runtime

[`plug/`](plug/) is a macOS-only Python service that watches Apple’s iMessage SQLite database and lets a Claude agent draft or send replies. It is intentionally split into two independently restartable processes:

```text
Apple chat.db ──► watchdog :8001 ──► ~/.plug/spool.db ──► supervisor :8002 ──► Messages.app
                     read-only                         leased queue              AppleScript
```

- `watchdog` reads `chat.db` with a read-only connection, filters inbound messages, advances a cursor, and enqueues work.
- `supervisor_agent` leases work, coalesces bursts, applies rate limits and safety rules, calls the model, and sends through AppleScript.
- The SQLite spool provides idempotent enqueue, leases, retries, dead-lettering, and backlog visibility.
- Both services expose health/status/Prometheus endpoints and write privacy-conscious structured events to `~/.plug/events.jsonl`.

See [`plug/README.md`](plug/README.md) for the detailed setup, permissions, safety behavior, endpoints, and troubleshooting guide.

## Current status

Implemented now:

- Static Sediment shared-space and backstage demo.
- Watchdog/supervisor split with a durable leased queue.
- Dry-run mode, kill switch, per-chat/global rate limits, loop guard, OTP filters, and AppleScript argument safety.
- HTTP and CLI controls, Prometheus text endpoints, structured event logging, and a broad test suite.

Not implemented yet:

- A persistent backend/API connecting the Sediment UI to live agent state.
- Port catalog synchronization and architecture scorecards.
- Bright Data web-data ingestion.
- OpenTelemetry export and SigNoz dashboards/alerts.

The implementation plans are in [`docs/`](docs/):

1. [`port-architecture-plan.md`](docs/port-architecture-plan.md) — model services, queues, agents, environments, ownership, and readiness checks in Port.
2. [`bright-data-plan.md`](docs/bright-data-plan.md) — add an optional, privacy-scoped web-research path using Bright Data Scraper Studio.
3. [`signoz-observability-plan.md`](docs/signoz-observability-plan.md) — instrument the Python services with OpenTelemetry and monitor latency, errors, queue age, and delivery outcomes in SigNoz.

## Quick start

The runtime requires macOS, Full Disk Access for the watchdog, Automation permission for Messages.app, and an Anthropic API key. Start safely in dry-run mode:

```bash
cd plug
uv sync
cp .env.example .env       # set ANTHROPIC_API_KEY
uv run plug doctor
uv run uvicorn watchdog.main:app --port 8001
```

In another terminal:

```bash
cd plug
PLUG_DRY_RUN=1 uv run uvicorn supervisor_agent.main:app --port 8002
uv run plug tail -f
```

Confirm that the supervisor reports `"dry_run": true` at `http://localhost:8002/health` before allowing live sends. Run tests with:

```bash
cd plug
uv run pytest
```

## Planned target architecture

```text
public web sources ─► Bright Data ─► normalized evidence/artifact
messages ─► watchdog ─► durable spool ─► supervisor/agent ─► shared-space API/UI
               │              │                 │                 │
               └──────────────┴─────────────────┴─────────────────┘
                              OpenTelemetry ─► SigNoz

Port catalogs the services, ownership, dependencies, environments, and readiness scorecards around this system.
```

Bright Data is an optional enrichment source, not a replacement for the message pipeline. It must never receive private message bodies unless an explicit product decision and data-processing policy allow that. All credentials belong in environment/secret management, never in `plug.yml`, the UI, or committed Markdown.

## External references

- [Port documentation](https://docs.port.io/) — blueprints, relations, scorecards, actions, and data ingestion.
- [Bright Data Scraper Studio](https://brightdata.com/products/scraper-studio) and [Collection API quickstart](https://docs.brightdata.com/datasets/scraper-studio/quickstart) — production collectors and trigger/delivery options.
- [SigNoz documentation](https://signoz.io/docs/what-is-signoz/) and [Python instrumentation](https://signoz.io/docs/instrumentation/python/) — OpenTelemetry-based logs, metrics, traces, dashboards, and alerts.

## Safety boundary

This is hackathon-grade software. The HTTP endpoints are unauthenticated by default, the queue is local SQLite, and live mode can send real messages. Keep services bound to localhost, start with dry-run, restrict `chats.only_handles`, and use the supervisor pause endpoint before testing changes.
