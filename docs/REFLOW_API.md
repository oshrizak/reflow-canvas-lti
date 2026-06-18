# Reflow Core API consumption

The connector is a CLIENT of Reflow Core's HTTP API. It does not import any
Reflow Core Python code. This doc enumerates the endpoints it actually calls
and the response shapes it depends on.

All calls go through `connector/canvas/reflow_client.py::ReflowClient`. The
base URL comes from `REFLOW_API_BASE_URL`; the bearer key from `REFLOW_API_KEY`
(sent as `X-API-Key`).

## Endpoints

### POST `/api/v1/documents/submit`

Submit a document for accessibility conversion.

**Request** — multipart form:
- `file` — the document bytes. Connector infers Content-Type from extension
  (PDF / DOCX / PPTX / HTML / EPUB). Falls back to `application/octet-stream`.
- `review_mode` — `"human"` (default; lands in awaiting_review) or `"auto"`.
- `skip_pii_scan` (optional `"true"`) — bypass PII detection. The connector
  sets this for Canvas-uploaded course material, paired with
  `skip_reason="Canvas-uploaded course material"`.

**Response** — `{"job_id": "<uuid>"}`.

Called by:
- `connector/workers/canvas_watcher.py` — every discovered file.
- The smoke test in [PILOT_RUNBOOK.md](PILOT_RUNBOOK.md).

### GET `/api/v1/documents/{job_id}`

Return the job's current status + result payload.

**Response** — JSON. The connector reads:

| Field | When | Used for |
|---|---|---|
| `status` | always | One of `processing`, `pii_scanning`, `awaiting_approval`, `completed`, `failed`, `denied` |
| `markdown_url` (or `result_url`) | `status=completed` | Presigned S3 URL the bridge fetches |
| `figures` (or `stored_figures`) | `status=completed` | List of `{figure_id, url}`; bridge reuploads to Canvas folder |
| `pdf_classification` | `status=completed` | Conversion-quality signal input |
| `ocr_applied` / `ocr_was_run` | `status=completed` | Signal input |
| `error` | `status in (failed, denied)` | Surfaced in the panorama dial |

Called by:
- `connector/workers/reflow_bridge_worker.py::_drive_job` — every poll tick
  for every non-terminal canvas job. Retried with exponential backoff
  (`utils/retry_helpers.retry_with_backoff`).

### GET `{markdown_url}`

The presigned S3 URL Reflow Core returned in `markdown_url`. Connector fetches
it with no auth header (S3 presigned auth lives in the query string). The URL
is run through `rewrite_presigned_url()` first, which swaps the public S3
hostname for `S3_INTERNAL_URL` when set — needed when the connector and the
S3 service are both inside Docker but the presigned URL was issued against
the public hostname.

### POST `/api/v1/documents/{job_id}/pii/approve` *(pending upstream)*
### POST `/api/v1/documents/{job_id}/pii/deny` *(pending upstream)*

Record a faculty decision on flagged PII. **These endpoints are pending in
upstream Reflow Core** — the connector calls them via
`ReflowClient.submit_pii_decision()`; today they return 404. See the
"Known follow-ups" section of [CHANGELOG.md](../CHANGELOG.md).

**Request** — JSON: `{"justification": "<str ≥ 10 chars>", "reviewed_by": "<user_id_or_email>"}`.

**Response** — the post-transition status payload.

**Error contract:**
- 404 → connector raises `ReflowApiError(status_code=404)` with a message that
  points operators at the upstream PR.
- 409 → connector raises `ReflowApiError(status_code=409)` and the panorama
  handler returns 409 to the UI ("job already advanced past awaiting_approval —
  another instructor decided in parallel").
- Other 4xx/5xx → 502 with the upstream message.

Called by:
- `connector/api/canvas_panorama.py::pii_decision` — the faculty PII gate.

## Auth model

The connector sends `X-API-Key: ${REFLOW_API_KEY}` on every call (except
fetches of presigned S3 URLs). When `REFLOW_API_KEY` is unset, the header
is omitted entirely — only safe against a Reflow Core whose API key auth is
disabled.

`reflow_client.py::_default_api_key()` reads `settings.reflow_api_key` as
the source of truth. The setting is `SecretStr`; the client calls
`get_secret_value()` once at construction and stores the resulting bytes,
so it never appears in log lines.

## Out of scope for the connector

The connector deliberately does NOT consume:

- `/api/v1/documents/{id}/approval/*` (core's full approval flow) — the
  connector forwards just the PII decision, not the publication decision.
  Publication state lives entirely in the connector + Canvas.
- `/api/v1/feedback/*` — feedback aggregation lives in core.
- Any pipeline-internal endpoint like `/api/v1/pipeline/*`, `/api/v1/jobs/*`,
  `/api/v1/agents/*`. Those are core's internals; the connector does not
  reason about them.

If a Canvas integration ever needs information from one of those, the right
move is a small upstream PR adding a stable HTTP shape that the connector
can call.
