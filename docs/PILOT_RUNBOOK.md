# Pilot runbook

Steps for running a Canvas pilot end-to-end against the connector, plus the
failure modes that show up most often during the first week.

## Pre-flight

Before the first faculty launch:

1. **Reflow Core is reachable.** From the connector host, `curl
   $REFLOW_API_BASE_URL/health` returns `{"status":"healthy"}`. If it doesn't,
   the connector boots fine but the watcher silently piles up failed jobs.
2. **Reflow API key is set.** `curl -H "X-API-Key: $REFLOW_API_KEY"
   $REFLOW_API_BASE_URL/api/v1/documents/submit` should return 422 (missing
   file), not 401. A 401 means the key the connector has is wrong.
3. **LTI keypair exists.** `ls keys/` should show `lti_private.pem` and
   `lti_public.pem`. If not, `./scripts/generate_lti_keys.sh`.
4. **Canvas Developer Keys configured.** See [CANVAS_SETUP.md](CANVAS_SETUP.md).
5. **`CANVAS_WATCHED_COURSES`** lists the course IDs you want scanned (single
   tenant), OR `MULTI_TENANT_WATCHER=true` (multi-tenant — watcher iterates
   registered platforms).

## Local end-to-end smoke

Easiest verification the connector talks to a running Reflow Core:

```bash
docker compose up -d
docker exec reflow-canvas-lti-connector-1 python -c "
import asyncio, io
import pikepdf
from connector.canvas.reflow_client import ReflowClient

pdf = pikepdf.new()
pdf.add_blank_page(page_size=(612, 792))
buf = io.BytesIO(); pdf.save(buf)

async def main():
    rc = ReflowClient()
    job_id = await rc.submit_document(
        'smoke.pdf', buf.getvalue(),
        review_mode='auto', skip_pii_scan=True,
    )
    print('job_id =', job_id)
    print('status =', (await rc.get_status(job_id)).get('status'))

asyncio.run(main())
"
```

A reachable Reflow Core prints a UUID job_id and `status = 'processing'`.

## Common failure modes

### Watcher log: `Canvas watcher started with no watched courses; idling`

Expected when `CANVAS_WATCHED_COURSES=` is empty and
`MULTI_TENANT_WATCHER=false`. Either populate the list or flip the flag.

### Bridge log: `failed to fetch markdown … host not reachable`

Reflow Core returned a presigned URL pointing at a hostname the connector
can't reach. Set `S3_INTERNAL_URL` to the hostname the connector can reach
(e.g. `http://floci:4566` inside Docker) and `S3_PUBLIC_URL` to the host
the URL was issued against (e.g. `http://localhost:4566`). The bridge
rewrites the host before fetching.

This is the 2026-06-17 bug from the source fork — the field was missing
from Settings entirely. The connector ships it; just set it.

### Faculty PII decision returns 502 with "Reflow Core did not expose POST /api/v1/documents/.../pii/..."

Reflow Core hasn't yet shipped the PII approve/deny endpoints. Track the
upstream PR (`PORTING_BRIEF.md` "After push: small Core Reflow PRs"). Until
then, faculty PII gates need to be resolved directly in Reflow Core's own
UI (the pipeline viewer page).

### `/canvas/panorama/...` returns 401 "No LTI session"

Faculty cookie expired or the panorama overlay reached the endpoint
without a `reflow_lti_session` cookie. Have the faculty re-open the
"Accessible Documents" tool from the course navigation to refresh.

### Canvas Page write returns `invalid_scope`

The OAuth token the bridge is using does not carry
`url:POST|/api/v1/courses/:course_id/pages`. Cause is usually that the
Canvas **API** Developer Key (not the LTI key) wasn't toggled with
"Enforce Scopes" + the Pages scope. Fix on the Canvas side, then the
bridge self-heals on the next tick — no manual replay needed because
the bridge keeps `page_failed` jobs in its pollable set.

### `Job <id> exceeded 180s; moving on`

A single bridge tick on a job exceeded the per-job timeout. Logged but
non-fatal — the next tick picks it up. If a job hits this repeatedly,
inspect Canvas API latency or the size of figure uploads.

### `Bridge: job <id> failed to drive; moving on`

A genuine exception in the bridge for one job. Inspect the logged
traceback. The tick continues with subsequent jobs, so a single broken
job never strands the queue.

## Monitoring

The connector exposes Prometheus metrics on `${METRICS_PORT}` (default
`8001`) when `ENABLE_METRICS=true`. Wire it into the same Prometheus the
upstream Reflow Core stack uses; Grafana dashboards in the source fork
already cover the join.

Watch for:

- Reflow `submit_document` p95 latency. A creep above ~2s on small PDFs
  usually means Reflow Core's docling-serve is overloaded.
- Canvas API 429 / 5xx rate. The connector retries with exponential
  backoff, so individual blips are silent; a sustained climb shows up
  here.
- Worker tick duration. The watcher should finish a tick in seconds even
  with thousands of files (it scans incrementally). A tick longer than
  the configured `CANVAS_POLL_SECONDS` means the next one starts late.

## Rolling back

The connector keeps no irreversible state outside Canvas Pages it
created. To roll back a pilot:

1. Stop the connector (`docker compose down`).
2. Optionally, instruct the bridge to mark every Reflow-created Canvas
   Page unpublished by running a small one-off against
   `CanvasClient.unpublish_page` for each `canvas_page_url` recorded
   under `eq-pdf:canvas:job:*`.
3. Disable the LTI tool in Canvas (Developer Keys → toggle OFF).

Redis-stored Canvas job state can be retained for audit (default
`CANVAS_JOB_RETENTION_DAYS=90`).
