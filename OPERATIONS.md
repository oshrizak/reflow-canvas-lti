# Operations runbook

This document covers what an operator needs to know to keep the
connector running in production — secrets, backups, common breakage
modes, and the recovery procedures we worked out during the CSU East
Bay pilot.

Treat this as the canonical reference. If you fix something not
covered here, add it.

## Secrets checklist (before first launch)

Before exposing the connector to faculty, walk this list. Every empty
or placeholder value is either a 503 in the UI or a security finding.

| Variable | Required? | What breaks if missing |
|---|---|---|
| `REFLOW_API_KEY` | **Yes** | All document submissions 401 at Reflow Core. The placeholder `your-secret-key-here` is rejected unless your Reflow Core is also using it. |
| `TOKEN_ENCRYPTION_KEY` | **Yes** | OAuth tokens (= instructor impersonation credentials) get encrypted with a hardcoded fallback key. Anyone with the source can decrypt your Redis dump. Generate with `python -m connector.tools.generate_keys`. |
| `CSRF_SECRET_KEY` | **Yes** | CSRF tokens signed with a derivation of the LTI keypair fingerprint — stable but not a secret. Same generator. |
| `LTI_CLIENT_ID` | **Yes** | Every LTI launch fails with `Unexpected audience` — pull from the Canvas Developer Key. |
| `LTI_DEPLOYMENT_ID` | **Yes** | Every launch fails with `Unexpected deployment_id` — pull from Canvas's tool placement. Different per Canvas install of the same key. |
| `LTI_PRIVATE_KEY_PATH` + the actual PEM at that path | **Yes** | JWT signing fails. Generate with `scripts/generate_lti_keys.sh`; mount under `/app/keys`. |
| `CANVAS_OAUTH_CLIENT_ID` + `CANVAS_OAUTH_CLIENT_SECRET` | **Yes** | Per-instructor OAuth consent flow can't start; bridge falls back to LTI service tokens which Canvas Cloud rejects for `/api/v1/...` writes. |
| `CANVAS_ALLOWED_ORIGINS` | **Yes** | The panorama overlay's `fetch` calls from Canvas's origin are blocked by CORS, so the dial loads but nothing works. Include EVERY Canvas host faculty might be on (production, beta, test). |
| `LTI_PUBLIC_URL` | **Yes** | Canvas redirects mismatch; the LTI config endpoint serves `http://` URLs Canvas refuses. Must be HTTPS in prod. |
| `S3_PUBLIC_URL` + `S3_INTERNAL_URL` | If S3 hostnames differ inside vs outside Docker | Figures and markdown fetch 404; the bridge worker's figure upload fails. |
| `REQUIRE_WCAG_GATE` | Should be `true` in prod | When false, the approve handler runs WCAG checks but does NOT enforce them — faculty can publish a page with `error`-severity findings. |
| `ANTHROPIC_API_KEY` | Only if Translate is visible | Clean 503 on Translate clicks. Leave empty to disable. |
| `AWS_DEFAULT_REGION` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | Only if Audio MP3 is visible | Clean 503 on Audio MP3 clicks. IAM user needs `polly:SynthesizeSpeech`. |

Run `git diff .env.example .env` after every dependency bump to catch
new keys you haven't filled in.

### Generating the cryptographic keys

```bash
docker compose run --rm connector python -m connector.tools.generate_keys
```

Prints `TOKEN_ENCRYPTION_KEY=…` and `CSRF_SECRET_KEY=…` lines using
`secrets.token_urlsafe(32)` (256 bits of entropy each). Paste them
into `.env` and `docker compose restart connector`.

The startup logs include a `startup secrets audit` line — `OK` when
everything's set, `CRITICAL` per missing secret otherwise. Grep for
it after every release to verify a production deploy.

### Key rotation

* **TOKEN_ENCRYPTION_KEY rotation** invalidates every encrypted OAuth
  token in Redis — faculty must re-consent on next launch. Plan a
  maintenance window and post a notice to faculty before rotating.
* **CSRF_SECRET_KEY rotation** invalidates outstanding CSRF tokens —
  expected; clients re-fetch on the next state-changing call. Safe to
  do in-place.

## Redis persistence + backups

### What's in Redis (= what we lose if it goes away)

Every state-bearing piece of the connector lives in Redis:

* `eq-pdf:canvas:job:<id>` — per-job state (status, page URL, VeraPDF
  audit, figure URLs, instructor user id). Driving record for the dial.
* `eq-pdf:canvas:course:<id>:pending` — accessibility + PII review queue.
* `eq-pdf:canvas:course:<id>:processed` — idempotency marker so the
  watcher doesn't re-submit known files.
* `eq-pdf:lti:platform:<id>` — registered Canvas LTI tools.
* `eq-pdf:lti:user-token:<platform>:<user>` — instructor OAuth tokens.
* `eq-pdf:canvas:approval:audit` — append-only approval/PII audit log.
  **Legally sensitive** for accessibility complaints; retain per your
  records policy.

Losing Redis means losing every faculty consent, every approval, and
the entire audit trail. Reflow Core can re-process source PDFs but
cannot restore decisions.

### Persistence is now ON by default

`docker-compose.yml` runs Redis with:

* `--appendonly yes --appendfsync everysec` — every write durable
  within ~1 second of the request.
* Three RDB save points (default Redis snapshotting).
* Data dir mounted at the named volume `redis-data`.

`docker compose down` keeps the volume. **`docker compose down -v` wipes
everything**, including the audit log. Never run that on production
without a verified backup.

### Backup

Automated via the bundled script — schedule it on the host:

```bash
./scripts/backup-redis.sh
```

The script triggers a fresh `BGSAVE`, polls `LASTSAVE` until the
snapshot lands, `docker cp`s the new `dump.rdb` into `./backups/`
with a UTC timestamp in the filename, and prunes anything in that
directory older than `BACKUP_RETENTION_DAYS` (default 14).

For real off-host durability set `BACKUP_S3_BUCKET` in the
environment and have the AWS CLI on PATH with credentials:

```bash
export BACKUP_S3_BUCKET=s3://my-bucket/reflow-redis
./scripts/backup-redis.sh
```

Cron entry (every 6 hours, ample for the connector's write rate):

```cron
0 */6 * * * cd /path/to/reflow-canvas-lti && ./scripts/backup-redis.sh >> backups/backup.log 2>&1
```

Or as a systemd timer if you prefer a structured unit. Pair the S3
bucket with a 30-day lifecycle policy → Glacier transition so long-
term cost stays bounded.

The `redis-data` named volume on its own only survives container
restarts, not host failures — never skip the off-host upload in
production.

Manual one-shot (without the script) if you need to investigate:

```bash
docker compose exec redis redis-cli BGSAVE
# Wait for "Background saving terminated with success" in `docker compose logs redis`
docker cp $(docker compose ps -q redis):/data/dump.rdb ./backups/redis-$(date +%Y%m%dT%H%M%S).rdb
```

### Restore

1. Stop the connector and Redis: `docker compose down` (NOT `down -v`).
2. Copy the backup `dump.rdb` (or AOF directory) back into the
   `redis-data` volume.
3. `docker compose up -d redis` and watch the logs — Redis prints
   `DB loaded from append only file` (or the equivalent for RDB) on
   successful restore.
4. Bring the connector back up: `docker compose up -d connector`.

## Rate limiting

Every state-changing POST/PUT is rate-limited per ``(endpoint, user_id)``
via a Redis-backed fixed-window counter. The limits are picked well
above legitimate faculty workflow but well below abuse:

| Endpoint | Bucket | Limit | Window |
|---|---|---|---|
| ``POST /canvas/panorama/approve/{job}`` | ``approve`` | 30 | 60s |
| ``POST /canvas/panorama/reject/{job}`` | ``reject`` | 30 | 60s |
| ``POST /canvas/panorama/request-edits/{job}`` | ``request_edits`` | 30 | 60s |
| ``POST /canvas/panorama/unpublish/{job}`` | ``unpublish`` | 30 | 60s |
| ``POST /canvas/panorama/pii-decision/{job}`` | ``pii_decision`` | 10 | 60s |
| ``POST /canvas/panorama/convert/{file}`` | ``convert`` | 30 | 60s |
| ``POST /canvas/panorama/approve/_bulk`` | ``approve_bulk`` | 5 | 60s |
| ``PUT  /canvas/panorama/edit/{job}`` | ``edit`` | 60 | 60s |
| ``POST /canvas/review/{job}/approve`` | ``review_approve`` | 30 | 60s |
| ``POST /canvas/review/{job}/reject`` | ``review_reject`` | 30 | 60s |

When the limit is exceeded the response is **429 Too Many Requests**
with a ``Retry-After`` header set to the seconds until the next window
opens. The limiter logs a ``WARNING`` line every time it fires:

```
rate limit exceeded: bucket=approve actor=<user_id> count=31 limit=30 window=60s
```

Counters live at ``eq-pdf:rl:<bucket>:<actor>:<window_id>`` and
auto-expire 5 seconds after the window closes, so the namespace stays
tidy without a sweeper.

To raise a specific limit (e.g. course migration day with bulk
approvals), grep the relevant ``enforce_rate_limit`` call in
``connector/api/canvas_panorama.py`` or ``canvas_review.py`` and bump
the ``limit=`` value, then ``docker compose restart connector``.

## Common breakage modes (from CSUEB pilot)

### "Unexpected deployment_id" at every launch

You re-registered the LTI tool in Canvas. The `LTI_DEPLOYMENT_ID` env
var no longer matches the new deployment. Either:

* Update `.env`'s `LTI_DEPLOYMENT_ID` to the new value (read it from
  Canvas's tool placement) and migrate Redis data from the old
  platform_id to the new one (compute via
  `connector.lti.platform.compute_platform_id`). Migration script
  pattern lived in this session's chat — distill into a CLI under
  `connector/tools/` if you re-encounter this.
* Or revert to the old deployment if it still exists in Canvas.

### `invalid_scope` on every Canvas write

The Canvas Developer Key is missing scopes the bridge needs:

* `url:POST|/api/v1/courses/:course_id/pages`
* `url:PUT|/api/v1/courses/:course_id/pages/:url_or_id`
* `url:POST|/api/v1/courses/:course_id/files`
* `url:POST|/api/v1/conversations`

Add them in Canvas → Developer Keys → Edit → Enforce scopes, then
**force faculty to re-consent** (their stored OAuth tokens have only
the scopes from their consent time, not the current key state). Wipe
`eq-pdf:lti:user-token:*` to force the OIDC flow to redirect through
the consent screen on the next launch.

### "Failed to fetch" on PII Approve

Reflow Core's PII gate is approval-token-based: `POST
/api/v1/approval/{approval_token}/decision`. The connector now hits
that route (see `submit_pii_decision` in `connector/canvas/reflow_client.py`).
If you see 405 from Core again, Core's surface drifted — confirm via
`curl <core>/openapi.json | grep approval`.

### File processed but no dial on the row

The panorama overlay reads `scored_files` keyed by FILENAME. If the
filename in Canvas doesn't match what the job recorded (e.g., faculty
renamed the file), the dial won't attach. Check
`docker compose exec redis redis-cli get eq-pdf:canvas:job:<id>` for
the stored `canvas_file_name` and compare to what the panorama
overlay sees on the page.

### Searchable PDF takes a long time and the row dial flickers

WeasyPrint + matplotlib + PyMuPDF all run synchronously in the request
handler. A textbook chapter with 50+ figures and equations can take
20–40 seconds. Acceptable; the click downloads in-place (no blank
tab) so faculty sees the browser's download indicator immediately.
Consider moving to a background-job queue if 95p latency becomes a
problem.

### Audit log too big

The bridge worker calls `purge_old_canvas_records` on a slow timer
with `CANVAS_AUDIT_RETENTION_SECONDS` as the cutoff. Default 5 years.
Tune up or down per your records policy. Setting it to 0 disables
the audit purge entirely (recommended unless retention is a legal
problem).

## Health + observability

* Container HEALTHCHECK hits `GET /health` every 30s. A failing
  health check restarts the container under `restart: unless-stopped`.
* No Prometheus `/metrics` endpoint yet (dependency is in
  `pyproject.toml` but the route isn't mounted). When you add it,
  expose it on a separate port so the public Cloudflare tunnel
  doesn't see it.
* No structured logs yet. Use `docker compose logs --since=10m
  connector | grep -E "(WARNING|ERROR)"` for fast triage.

## What still wants attention before scaling beyond one course

* **OAuth tokens are stored in plaintext.** Encrypt at rest with
  Fernet keyed off an env secret.
* **No rate limiting on the state-changing POSTs.** A misbehaving
  browser extension can hit `/approve` or `/unpublish` thousands of
  times in a tight loop with valid CSRF.
* **No integration tests for the user-facing flows.** Unit tests
  exist for the pure logic; the LTI/CSRF/handler path has none.
* **Single uvicorn worker.** Fine for a single course at CSU East Bay
  pilot; not fine for a campus-wide rollout. Add `--workers N` and
  validate the in-process worker (watcher + bridge) is `worker 0`
  only (or move it to a separate process).
