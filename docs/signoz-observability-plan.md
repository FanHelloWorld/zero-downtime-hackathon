# SigNoz observability implementation plan

## Goal

Use [SigNoz](https://signoz.io/docs/what-is-signoz/) as the performance and reliability view for the watchdog, supervisor, future research worker, and shared-space API. SigNoz is OpenTelemetry-native and supports logs, metrics, traces, exceptions, dashboards, and alerts. The existing Prometheus endpoints and JSONL events are useful compatibility seams, but they are not yet SigNoz integration.

## Instrumentation strategy

Use OpenTelemetry for Python and export OTLP to SigNoz Cloud or a self-hosted collector. Follow the [SigNoz Python instrumentation guide](https://signoz.io/docs/instrumentation/python/) and instrument the ASGI apps with FastAPI/Uvicorn middleware where applicable. Add manual spans around the synchronous worker loops and external calls.

Set stable resource attributes:

```text
service.name = plug-watchdog | plug-supervisor | sediment-api | research-worker
service.version = package/git version
deployment.environment = local | staging | production
```

Do not put message text, phone numbers, chat GUIDs, model prompts, API tokens, or raw scraped content into span attributes. Use the existing short anonymous chat hash only when correlation is genuinely needed, and prefer aggregate dimensions.

## Metrics

Expose low-cardinality counters/histograms/gauges. Suggested names:

| Signal | Metric | Dimensions |
|---|---|---|
| ingestion | `plug_messages_seen_total`, `plug_messages_spooled_total`, `plug_messages_filtered_total` | `service`, `reason` |
| queue | `plug_queue_depth`, `plug_oldest_pending_age_seconds` | `service`, `state` |
| processing | `plug_batches_total`, `plug_processing_duration_seconds` | `service`, `outcome` |
| model | `plug_model_requests_total`, `plug_model_duration_seconds`, `plug_model_errors_total` | `model`, `error_type` |
| delivery | `plug_replies_total`, `plug_send_duration_seconds`, `plug_send_errors_total` | `outcome`, `strategy` |
| research | `research_jobs_total`, `research_duration_seconds`, `research_errors_total` | `collector`, `outcome` |
| runtime | `plug_loop_ticks_total`, `plug_tick_errors_total` | `service` |

Prefer metrics for counts and latency. Keep detailed reasons in structured logs, with bounded enumerations rather than arbitrary exception strings as metric labels.

## Traces and logs

Create a trace for each HTTP request and a root span for each message lifecycle:

```text
watchdog tick
  └─ chat.db read
  └─ spool enqueue
  └─ supervisor notify
supervisor batch
  └─ lease
  └─ model call
  └─ safety decision
  └─ AppleScript send
  └─ spool ack/nack
```

Convert `plug.events.emit()` into structured OpenTelemetry logs or bridge the JSONL tailer during the first phase. Preserve `stage`, `event`, `outcome`, and timing fields. Add trace/span IDs to logs so SigNoz can correlate logs and traces, as described in its [correlation guide](https://signoz.io/docs/traces-management/guides/correlate-traces-and-logs/).

## Dashboards and alerts

Create one service dashboard with watchdog poll errors, queue depth and oldest age, batch/model/send latency percentiles, retry/dead-letter/blocked/permanent-failure rates, dry-run/live mode, supervisor health, and (once Bright Data is enabled) research duration and collector failures.

Use SigNoz [alert rules](https://signoz.io/docs/userguide/alerts-management/) with explicit routing and maintenance windows:

- critical: watchdog or supervisor health absent for 2 minutes;
- critical: oldest pending queue item above 60 seconds in a live environment;
- warning: queue depth or oldest age rising for 10 minutes;
- warning: retry/dead-letter rate above baseline;
- warning: p95 model or send latency above the agreed SLO;
- critical: an unexpected live-send error burst;
- research warning: repeated collector 429/5xx or missing result delivery.

Tune thresholds after a staging baseline. Use missing-data alerts for a stopped service and avoid high-cardinality labels such as chat IDs or arbitrary URLs.

## Repository changes

1. Add OpenTelemetry Python dependencies to `plug/pyproject.toml`.
2. Add `plug/telemetry.py` for provider/resource/exporter setup and graceful shutdown.
3. Initialize telemetry in both ASGI lifespans and CLI entry points.
4. Add manual spans/metrics at watchdog ticks, spool operations, supervisor batches, model calls, AppleScript sends, and research jobs.
5. Add a `telemetry_enabled`/exporter configuration that defaults off for local tests.
6. Add unit tests for instrumentation boundaries and redaction; integration-test export with a local collector or mocked exporter.
7. Commit dashboard and alert definitions as code where the SigNoz deployment supports it.

## Acceptance criteria

- A test message can be followed from intake through queue, model, safety, and delivery in SigNoz.
- Queue age and service health remain visible when the supervisor is stopped.
- A failed send is searchable as a structured error and correlated with its trace.
- Alerts have tested routing, runbooks, and no sensitive payloads.
- Telemetry can be disabled without changing message behavior or breaking the test suite.
