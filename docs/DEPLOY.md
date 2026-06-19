# Deployment

The connector ships as a single Python service plus a Redis dependency. The
intended deployment shape is the same image (`Dockerfile`) running anywhere
that can reach:

- Reflow Core (`REFLOW_API_BASE_URL`).
- Canvas (`CANVAS_API_URL` + per-instructor OAuth endpoints).
- A Redis the connector owns.
- Reflow Core's S3 (presigned URL fetches).

## Image

```bash
docker build -t reflow-canvas-lti:0.1.0 .
```

The default `CMD` is `uvicorn connector.main:app --host 0.0.0.0 --port 8000`.
Health is checked via `GET /health`.

System packages installed (for alt-format generators):
- `tesseract-ocr` — OCR'd PDF alt format
- `ghostscript`, `qpdf`, `unpaper` — `ocrmypdf` dependencies
- `poppler-utils` — `pdf2image`

If you don't ship the OCR'd PDF alt format you can drop these to slim the
image — `ocrmypdf` and `pdf2image` Python wheels will still install but
runtime calls will fail with a clear error.

## Environment

`.env.example` is the source of truth. The minimum production set:

```
REFLOW_API_BASE_URL=https://reflow-core.your-org.edu
REFLOW_API_KEY=<from secret manager>

LTI_ENABLED=true
LTI_PUBLIC_URL=https://reflow-canvas-lti.your-org.edu
LTI_ISSUER=https://canvas.instructure.com
LTI_CLIENT_ID=...
LTI_DEPLOYMENT_ID=...
LTI_AUTH_LOGIN_URL=...
LTI_AUTH_TOKEN_URL=...
LTI_JWKS_URL=...

CANVAS_API_URL=https://canvas.instructure.com
CANVAS_OAUTH_CLIENT_ID=...
CANVAS_OAUTH_CLIENT_SECRET=<from secret manager>
CANVAS_ALLOWED_ORIGINS=https://canvas.instructure.com

# Production-required security keys — generate with
#   docker compose run --rm connector python -m connector.tools.generate_keys
TOKEN_ENCRYPTION_KEY=<from secret manager>
CSRF_SECRET_KEY=<from secret manager>

# Production-recommended: enforce the WCAG publication gate
REQUIRE_WCAG_GATE=true

REDIS_URL=redis://<host>:6379/0
LOG_LEVEL=INFO
ENVIRONMENT=production
```

Optional, only when the matching alt-format is enabled:

```
# Audio (MP3) — Amazon Polly
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=<from secret manager>

# Translate — Anthropic Claude
ANTHROPIC_API_KEY=<from secret manager>
```

For multi-tenant deployments, also set:

```
MULTI_TENANT_WATCHER=true
CANVAS_TENANT=<short slug, e.g. "csueb" or "uchicago">
```

## Secrets

- `LTI_PRIVATE_KEY_PATH` — mount the RSA private key as a read-only
  volume. Never bake it into the image.
- `REFLOW_API_KEY`, `CANVAS_OAUTH_CLIENT_SECRET`, `CANVAS_API_TOKEN` (if
  used) — pull from your secret manager into env at runtime.
- `TOKEN_ENCRYPTION_KEY`, `CSRF_SECRET_KEY` — same pattern. Required for
  production. The connector logs `CRITICAL` per missing key at startup
  via its boot-time secrets audit; grep `startup secrets audit:` after
  every deploy to verify cleanliness. See
  [`OPERATIONS.md`](../OPERATIONS.md#secrets-checklist-before-first-launch)
  for the full checklist and key-rotation procedure.

## Persistence + backups

`docker-compose.yml` runs Redis with AOF + RDB persistence on a named
volume (`redis-data`). The volume survives `docker compose down`;
`docker compose down -v` wipes it.

Schedule the bundled backup script from host cron for off-host
durability:

```
0 */6 * * * cd /path/to/reflow-canvas-lti && BACKUP_S3_BUCKET=s3://… ./scripts/backup-redis.sh
```

See [`OPERATIONS.md`](../OPERATIONS.md#redis-persistence--backups) for
the full backup/restore procedure.

## Network

- Inbound (from Canvas) — TCP 443 on the load balancer, terminated to TCP
  8000 on the container. HTTPS only; Canvas validates the redirect URL.
- Inbound (Theme Editor `/panorama.js`) — same path.
- Outbound to Reflow Core — TCP 443 typically.
- Outbound to Canvas (REST + OAuth) — TCP 443 to the Canvas host.
- Outbound to S3 / floci (presigned URLs) — TCP 443 / 4566 depending on
  environment.

## Scaling

The connector is stateless (Redis owns state). Scale horizontally behind
a load balancer — each replica runs its own watcher + bridge workers, so
add a coordination strategy if you grow beyond one replica:

- **Simplest** — run only one replica. The watcher tick is cheap.
- **Stronger** — set `DISABLE_WORKERS=true` on all-but-one replica so
  HTTP scales horizontally while workers stay singletons. *(Not yet a
  setting — file an issue if you need this.)*

## Health checks

- `GET /health` — liveness. Returns 200 once FastAPI startup completes.
- `GET /lti/healthz` — LTI-specific readiness; reports whether the
  module is enabled.

## Logging

`LOG_FORMAT=json` (default in production; ENVIRONMENT must be unset or
set to a non-`dev` value) emits one JSON object per log line, ready for
CloudWatch Insights, Loki, Datadog, etc. `LOG_FORMAT=text` is the
default for `ENVIRONMENT=dev`.

Context vars carried through async hops: `request_id`, `user_id`,
`course_id`, `tenant`.

## Metrics

Prometheus metrics on `${METRICS_PORT}` (default `8001`) when
`ENABLE_METRICS=true`. See [PILOT_RUNBOOK.md](PILOT_RUNBOOK.md) for
the dashboards that matter.
