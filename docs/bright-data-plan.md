# Bright Data Scraper Studio implementation plan

## Goal and boundary

Add optional public-web research to Sediment so an agent can gather current facts and leave a cited artifact in the shared space. Bright Data Scraper Studio is a hosted IDE/runtime for custom scrapers; its [Collection API](https://docs.brightdata.com/datasets/scraper-studio/quickstart) triggers a published collector and returns a collection ID, while results can be polled or delivered by webhook, object storage, or another supported destination.

This is not part of the current `plug` message-reply path. The first implementation should be a separate research worker with a narrow input/output contract. Do not send private iMessage text to Bright Data by default; accept only explicit public URLs, search terms, or user-approved query fields.

## Proposed flow

```text
agent/tool request ─► research job ─► Bright Data collector
                         │                  │
                         │ collection_id   ▼
                         └──────────────► result fetch/webhook
                                             │
                                             ▼
                                validate + normalize + cite
                                             │
                                             ▼
                                      shared-space artifact
```

## Collector contract

Create and publish one narrowly scoped collector first, for example `public_research_v1`:

```json
{
  "inputs": [{"url": "https://example.com", "question": "..."}],
  "output": {
    "title": "string",
    "source_url": "string",
    "retrieved_at": "RFC3339 timestamp",
    "facts": [{"claim": "string", "evidence": "string"}],
    "errors": ["string"]
  }
}
```

The collector should return source URLs and retrieval timestamps for every claim. Keep raw HTML out of the message prompt unless needed; store only normalized evidence needed for the artifact and a link to the source.

## Repository changes

Add a `research/` module under `plug/` (or a separately deployed worker) with:

- `client.py`: bearer-authenticated trigger and dataset retrieval;
- `jobs.py`: job state, collection ID, retries, timeout, and idempotency key;
- `normalize.py`: schema validation, URL normalization, size limits, and citation checks;
- `server.py`: authenticated webhook endpoint if webhook delivery is selected;
- `artifact.py`: writes a redacted artifact event for the future shared-space API;
- tests using mocked Bright Data responses, never live credentials.

Configuration should be environment-only:

```text
BRIGHT_DATA_API_TOKEN
BRIGHT_DATA_COLLECTOR_ID
BRIGHT_DATA_WEBHOOK_SECRET   # if webhook delivery is used
```

Use Bright Data’s documented API pattern: `POST /dca/trigger` with a JSON array of collector inputs, retain the returned `collection_id`, then poll `GET /dca/dataset?id=...` with bounded exponential backoff. Prefer webhook delivery when the deployment can expose a verified endpoint; polling is the simpler first milestone.

## Safety and data policy

- Permit public sources only unless the user explicitly approves another source.
- Validate `https` and block localhost/private-network targets to prevent SSRF.
- Apply per-job URL, result-size, time, and cost limits.
- Treat scraped text as untrusted prompt input; defend against prompt injection and preserve source boundaries.
- Redact credentials, cookies, phone numbers, and message bodies from logs and artifacts.
- Record provenance, collector version, request time, and source URL.
- Retry 429/5xx responses with backoff; do not retry malformed inputs or authentication failures indefinitely.
- Keep the feature disabled by default until credentials and billing limits are configured.

## Delivery phases

1. Build/publish a collector manually and validate its output schema with sample public URLs.
2. Implement the client and a CLI command that triggers a job and writes a local JSON artifact.
3. Add job persistence and idempotency using the existing spool pattern or a dedicated research table.
4. Add a supervisor tool with explicit user-approved inputs and citation validation.
5. Connect normalized artifacts to the Sediment shared-space API.
6. Add Port catalog metadata and SigNoz telemetry for job latency, failures, and spend-related counters.

## Acceptance criteria

- A public URL can produce a cited artifact without blocking the watchdog or supervisor loops.
- A repeated request does not create duplicate artifacts.
- Invalid URLs, timeout, 429, 5xx, malformed output, and auth errors have distinct terminal/retry behavior.
- Tests prove that private message content and secrets never enter the Bright Data request or telemetry.
