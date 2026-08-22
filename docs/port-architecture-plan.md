# Port architecture plan

## Goal

Use [Port](https://docs.port.io/) as the living architecture and ownership catalog for Sediment/Plug. Port should describe what runs, where it runs, who owns it, what it depends on, and whether it meets the minimum production-readiness bar. It is a planning and governance layer; it does not replace the runtime queue or SigNoz.

Port blueprints represent assets with properties and relations, while scorecards evaluate standards across catalog entities. See Port’s [data model](https://docs.port.io/context-lake/data-model/configure-data-model/) and [scorecards](https://docs.port.io/governance/standards-and-compliance/overview/).

## Catalog model

Create these blueprints with stable identifiers so sync jobs are idempotent:

| Blueprint | Important properties | Relations |
|---|---|---|
| `sediment_service` | `identifier`, `repository`, `runtime`, `lifecycle`, `criticality`, `owner`, `health_url` | `depends_on` `runtime_component`; `owned_by` `team` |
| `runtime_component` | `identifier`, `kind`, `environment`, `endpoint`, `data_access`, `status` | `part_of` `sediment_service` |
| `agent` | `identifier`, `agent_type`, `state`, `tools`, `quiet_policy`, `owner` | `runs_on` `runtime_component`; `writes_to` `artifact` |
| `artifact` | `identifier`, `title`, `state`, `provenance_url`, `created_at`, `sensitivity` | `produced_by` `agent` |
| `data_source` | `identifier`, `kind`, `public_or_private`, `retention`, `provider` | `consumed_by` `agent` |
| `environment` | `identifier`, `kind`, `region`, `lifecycle` | related to services/components |

Initial entities should include `sediment-ui`, `plug-watchdog`, `plug-supervisor`, `spool-sqlite`, `messages-app`, `bright-data-research`, and `signoz`.

## Architecture relationships

```text
plug-watchdog ──reads──► Apple chat.db
plug-watchdog ──writes──► spool-sqlite
plug-supervisor ──leases──► spool-sqlite
plug-supervisor ──sends via──► Messages.app
plug-supervisor ──calls──► Anthropic API
sediment-ui ──reads (planned)──► shared-space API
bright-data-research ──produces (planned)──► evidence/artifact
all runtime components ──export telemetry──► SigNoz
```

## Scorecards

Start with a `zero_downtime_readiness` scorecard on `sediment_service` and `runtime_component`:

- has an owner and escalation contact;
- has a health endpoint and documented runbook;
- has a safe deployment/restart procedure;
- exposes queue depth/oldest-item age where applicable;
- has dry-run or staging coverage before live sends;
- has secrets outside source/config files;
- emits telemetry and has a SigNoz dashboard;
- has a rollback or pause control;
- documents data access and retention;
- has tests for failure/retry behavior.

Use Port actions later for safe operations such as opening a runbook, pausing the supervisor, or linking to the relevant SigNoz dashboard. Do not expose unauthenticated runtime controls through Port until the service endpoints have authentication and authorization.

## Delivery phases

1. Create blueprints and seed the initial entities manually.
2. Add repository metadata and ownership through Port’s Git integration or API.
3. Add a sync job that upserts service status from `/health` and `/status`; never send message content.
4. Add CI updates for test status, deployment version, and scorecard properties.
5. Add scorecards and dashboards, then make readiness visible in the hackathon demo.
6. Add authenticated Port actions only after the control plane is secured.

## Acceptance criteria

- A new contributor can find the watchdog/supervisor split, owners, endpoints, and runbooks in Port.
- The catalog shows the spool as a dependency and identifies its local durability limitation.
- A service fails the readiness scorecard when it has no health check, telemetry, owner, or rollback procedure.
- No private message body, API token, or Apple database content is ingested into Port.
