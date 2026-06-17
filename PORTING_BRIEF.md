# reflow-canvas-lti Porting Brief

**Goal:** Extract the working Canvas LTI integration out of the `equalify-reflow` fork and into a standalone connector service (`reflow-canvas-lti`) that consumes the upstream Reflow HTTP API. Mirror the architectural pattern of `equalify-reflow-wp`.

**Source of truth:** `C:\Users\hq1500\Desktop\equalify-reflow-main` — this is the fork that just successfully converted SPRITE Chimera, Audre Lorde Cancer Journals, and RFC 791 end-to-end against live Canvas at CSUEB on 2026-06-17. Use it as the canonical implementation; do NOT pull from older scaffolding at `Desktop\Reflow Canvas API`.

**Target location:** `C:\Users\hq1500\Documents\GitHub\reflow-canvas-lti` (new directory; you create it).

**Reviewer context:** The upstream Equalify maintainer asked to split this work out of the core Reflow repo because the Canvas integration as a fork of core makes Canvas a first-class concern of the core service. The split addresses that exact concern.

---

## Architecture target

```
       Canvas LMS                         Reflow Core (upstream)
       (CSUEB / etc.)                     equalify-reflow on main
            ↕                                       ↑
            ↕ LTI 1.3, OAuth, REST              HTTP API
            ↕                                       │
     reflow-canvas-lti  ────────────────────────────┘
     (this new repo)        POST /api/v1/documents/submit
                            GET  /api/v1/documents/{id}
                            POST /api/v1/documents/{id}/pii/approve
                            GET  presigned S3 URLs
```

**Connector owns:**
- LTI 1.3 launch + OIDC flow
- Per-instructor Canvas OAuth tokens
- Canvas API client (files, pages, modules, discussions, etc.)
- Canvas file watcher (scans courses, discovers PDFs, submits to Reflow)
- Reflow bridge (polls Reflow status, builds Canvas Pages on completion)
- Panorama overlay (dial badges + faculty review modal injected into Canvas via Theme Editor)
- Faculty WYSIWYG editor + alt-text helper
- Axe-core WCAG checks on generated HTML
- Alt-format generators (HTML, ePub, OCR'd PDF, translation, Polly audio, braille)
- Per-job Canvas-side state (Redis with `eq-pdf:lti:*` and `eq-pdf:canvas:*` prefixes)

**Core Reflow owns (no changes needed for MVP):**
- Document conversion pipeline (Docling + pydantic-ai)
- PII detection (Microsoft Presidio)
- S3 result storage
- Public document API (`/api/v1/documents/*`)

---

## Migration map: what moves where

All paths are relative to source root `equalify-reflow-main/`.

### Backend Python (whole-file copy with import-path rewrites)

| Source | Destination | Notes |
|---|---|---|
| `src/lti/` (entire directory, 8 files) | `connector/lti/` | LTI 1.3 launch, OIDC, JWT validation, platform store, session, dev keys |
| `src/canvas/` (entire directory, 16 files) | `connector/canvas/` | Canvas client, OAuth, alt-formats, sanitize, wcag_checks, signals, reflow_client, etc. |
| `src/workers/canvas_watcher.py` | `connector/workers/canvas_watcher.py` | Polls Canvas, discovers files, submits via reflow_client |
| `src/workers/reflow_bridge_worker.py` | `connector/workers/reflow_bridge_worker.py` | **Refactor:** any `from ..services.job_service` calls become HTTP calls via `canvas/reflow_client.py` |
| `src/api/canvas_consent.py` | `connector/api/canvas_consent.py` | Faculty consent capture |
| `src/api/canvas_oauth.py` | `connector/api/canvas_oauth.py` | OAuth authorize/callback |
| `src/api/canvas_panorama.py` | `connector/api/canvas_panorama.py` | Panorama overlay endpoints |
| `src/api/canvas_review.py` | `connector/api/canvas_review.py` | Faculty review pages |

### Frontend assets (copy as-is)

| Source | Destination |
|---|---|
| `src/web/canvas_review/dashboard.html` | `connector/web/canvas_review/dashboard.html` |
| `src/web/canvas_review/index.html` | `connector/web/canvas_review/index.html` |
| `src/web/canvas_review/one.html` | `connector/web/canvas_review/one.html` |
| `src/web/canvas_review/panorama.js` | `connector/web/canvas_review/panorama.js` |

### Shared utilities (decide per-module)

`src/utils/retry_helpers.py`, `src/middleware/logging_middleware.py`, `src/shared/constants/*`, `src/shared/models/*`:
- Copy minimally — only what the LTI/Canvas modules actually import.
- Run `grep -rE "from \.\.shared|from \.\.utils|from \.\.middleware" src/lti src/canvas src/workers/canvas_watcher.py src/workers/reflow_bridge_worker.py src/api/canvas_*.py` to enumerate.

### Config: settings to bring over

From `src/config.py`, port these Settings fields into `connector/config.py`:
- All `lti_*` fields (lti_enabled, lti_issuer, lti_deployment_id, lti_client_id, lti_auth_login_url, lti_auth_token_url, lti_jwks_url, lti_state_ttl_seconds, lti_private_key_path, lti_public_key_path, lti_public_url)
- All `canvas_*` fields (canvas_api_url, canvas_api_token, canvas_watched_courses, canvas_poll_seconds, canvas_allowed_origins, canvas_oauth_client_id, canvas_oauth_client_secret)
- `multi_tenant_watcher`, `redis_url`, `api_keys`, `s3_public_url`, `s3_internal_url` ← **MUST INCLUDE s3_internal_url; this was a bug in the fork**
- A new `reflow_api_base_url` (default `http://localhost:8080` for local dev) — the connector will call this for document submission/status

### Main entrypoint

Create `connector/main.py` modeled after `equalify-reflow-main/src/main.py` but pruned to ONLY the connector concerns:
- FastAPI app
- Lifespan: start canvas_watcher + reflow_bridge_worker tasks
- Routers: lti, canvas_consent, canvas_oauth, canvas_panorama, canvas_review
- Static mount for `web/canvas_review/`
- Root-level `/panorama.js` route (Theme Editor loads it from root)
- CORS for Canvas origin
- DO NOT include: documents, approval, feedback, pipeline_viewer routers (those stay in core)

---

## Bridge worker refactor — the one real code change

The bridge currently calls a mix of:
- `reflow.get_status(job_id)` ← already HTTP, keep
- `reflow.fetch_markdown(url)` ← already HTTP, keep
- `services.job_service.get_job(...)` ← INTERNAL, must become HTTP

**The s3_internal_url field bug from this session must be fixed in the new repo:**
Add `s3_internal_url` to `connector/config.py` (we missed it in the fork — it's the bug that caused the bridge to fail to fetch markdown until we patched it on 2026-06-17). The `rewrite_presigned_url()` function in `canvas/reflow_client.py` reads `settings.s3_internal_url` and must find it set.

---

## Repo skeleton to create first

```
reflow-canvas-lti/
├── README.md                          # Architecture, install, dev workflow
├── LICENSE                            # AGPL-3.0 (matches upstream Reflow)
├── CHANGELOG.md
├── pyproject.toml                     # Python 3.11+, fastapi, httpx, redis, etc.
├── Dockerfile
├── docker-compose.yml                 # connector + redis (+ optional cloudflared)
├── docker-compose.dev.yml             # dev overrides (volume mounts, hot reload)
├── .env.example                       # template — every required env var, documented
├── .gitignore                         # Python, IDE, .env, keys/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                    # pytest, ruff, mypy
│   │   └── docker-build.yml          # build + push image
│   └── ISSUE_TEMPLATE/
├── connector/
│   ├── __init__.py
│   ├── main.py                       # FastAPI entrypoint
│   ├── config.py                     # Settings class
│   ├── dependencies.py               # Redis client, etc.
│   ├── lti/                          # (port from src/lti/)
│   ├── canvas/                       # (port from src/canvas/)
│   ├── api/                          # (port from src/api/canvas_*.py)
│   ├── workers/                      # (port from src/workers/canvas_watcher.py + reflow_bridge_worker.py)
│   ├── utils/                        # (minimal subset)
│   ├── shared/                       # (minimal subset)
│   ├── middleware/                   # (minimal subset)
│   └── web/canvas_review/            # (frontend assets)
├── tests/
│   ├── conftest.py
│   ├── unit/                         # (port relevant tests)
│   └── integration/
├── scripts/
│   ├── preflight.py                  # env validation
│   └── generate_lti_keys.sh
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEPLOY.md
│   ├── CANVAS_SETUP.md               # Dev Key, redirect_uris, scopes
│   ├── REFLOW_API.md                 # which endpoints connector consumes
│   └── PILOT_RUNBOOK.md
└── keys/                             # gitignored — LTI RSA keypair
    └── .gitkeep
```

---

## Step-by-step execution plan

Follow this order. After each step, run `pytest` and `python -c "import connector.main"` to ensure nothing's broken.

### Phase A: Skeleton (30 min)
1. `mkdir reflow-canvas-lti && cd reflow-canvas-lti && git init`
2. Create directory tree as above
3. Write `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `LICENSE` (AGPL-3.0)
4. Create empty `__init__.py` in each Python package
5. First commit: "scaffold reflow-canvas-lti repo structure"

### Phase B: Config + main.py (30 min)
1. Port Settings class with LTI/Canvas fields (see "Config" section above)
2. Add `s3_internal_url` field
3. Add `reflow_api_base_url` field
4. Write minimal `connector/main.py` that just spins up FastAPI with /health endpoint
5. Verify: `docker compose up` boots, `curl localhost:8080/health` returns 200
6. Commit: "wire up Settings + main entrypoint"

### Phase C: LTI module (1 hr)
1. Copy `src/lti/` → `connector/lti/`
2. Rewrite imports: `from ..config import settings` (depth doesn't change but module path does)
3. Register router in main.py: `app.include_router(lti.router)`
4. Verify: `/lti/config.json` and `/lti/jwks.json` return valid JSON
5. Commit: "port LTI 1.3 launch + JWKS endpoint"

### Phase D: Canvas module (1.5 hr)
1. Copy `src/canvas/` → `connector/canvas/`
2. Rewrite imports
3. Note: `canvas/alt_formats.py`, `canvas/markdown_to_html.py`, `canvas/sanitize.py`, `canvas/wcag_checks.py` are all part of this — copy them
4. Add unit tests pass against ported modules
5. Commit: "port Canvas client + OAuth + alt-formats"

### Phase E: API routes (1 hr)
1. Copy `src/api/canvas_consent.py`, `canvas_oauth.py`, `canvas_panorama.py`, `canvas_review.py` → `connector/api/`
2. Wire each router in main.py
3. Verify all routes load: `python -c "from connector.main import app; print([r.path for r in app.routes])"`
4. Commit: "port canvas API routes"

### Phase F: Workers (1.5 hr) — **MOST WORK HERE**
1. Copy `src/workers/canvas_watcher.py` → `connector/workers/`
2. Copy `src/workers/reflow_bridge_worker.py` → `connector/workers/`
3. **REFACTOR bridge worker:** audit every `from ..services` import. Replace with HTTP calls via `connector/canvas/reflow_client.py`.
4. Wire workers in main.py lifespan
5. Commit: "port watcher + refactor bridge to pure HTTP"

### Phase G: Frontend (30 min)
1. Copy `src/web/canvas_review/` → `connector/web/canvas_review/`
2. Mount static via FastAPI
3. Add root `/panorama.js` route
4. Commit: "port panorama overlay frontend"

### Phase H: Local smoke test (1 hr)
1. Boot upstream Reflow Core locally (the `equalify-reflow` clone at `~/Documents/GitHub/equalify-reflow`)
2. Boot connector via `docker compose up` from the new repo
3. Hit `/lti/config.json` — verify JSON
4. Test connector → Reflow with a curl: submit a small PDF via the connector's bridge path
5. Commit: "verified end-to-end against local Reflow core"

### Phase I: Documentation (1 hr)
1. Write `README.md` (architecture diagram, install, dev workflow, AGPL note)
2. Write `docs/ARCHITECTURE.md` (deeper dive, mirrors equalify-reflow-wp's pattern)
3. Write `docs/CANVAS_SETUP.md` (Dev Key creation, scopes, redirect_uris)
4. Write `docs/REFLOW_API.md` (which Reflow endpoints the connector calls)
5. Commit: "documentation"

### Phase J: Push (15 min)
1. Create new GitHub repo at `oshrizak/reflow-canvas-lti` (you create via GitHub UI — make it AGPL-3.0)
2. `git remote add origin git@github.com:oshrizak/reflow-canvas-lti.git`
3. `git push -u origin main`
4. Update reviewer PR comment with link to new repo

**Total estimated time:** 8-10 hours focused work. Realistic to land tomorrow if you start now.

---

## Things to NOT do

- **Do not** port `src/services/job_service.py`, `pii_service.py`, `document_processing_service.py`, `pdf_extractor.py`, `pii_analyzer.py`, or anything in `src/agents/`. Those stay in core Reflow.
- **Do not** copy `src/api/documents.py`, `approval.py`, `pipeline_viewer.py` — connector is a CLIENT of those, not an implementation.
- **Do not** pull alt-format generators from `src/services/`. The Canvas-specific alt-formats live in `src/canvas/alt_formats.py` (which DOES move).
- **Do not** copy the demo data, scripts/, briefs/, or session-debug tooling from the fork.

---

## Acceptance criteria (before pushing)

- [ ] `docker compose up` boots connector + redis without errors
- [ ] `curl http://localhost:8000/health` returns 200
- [ ] `curl http://localhost:8000/lti/config.json` returns valid LTI tool config JSON
- [ ] `curl http://localhost:8000/lti/jwks.json` returns valid JWKS
- [ ] `pytest tests/` passes (port relevant tests from fork)
- [ ] Connector can submit a PDF to a locally-running Reflow Core and receive a job_id
- [ ] Connector can poll Reflow Core for status and receive a status payload
- [ ] README explains the architecture and how to run locally
- [ ] LICENSE is AGPL-3.0
- [ ] No copy of upstream Reflow's `services/` or `agents/` code is present

---

## Open questions to answer before push

1. **Package name:** keep "connector" or rename to "reflow_canvas_lti" for clarity in imports? Recommend the latter.
2. **Redis: shared with Reflow Core or separate instance?** For local dev, separate is cleaner (no namespace collision risk). For production, depends on deployment.
3. **Authentication to Reflow Core API:** connector uses an API key. How is that provisioned in the connector's `.env`?
4. **Multi-tenant story:** the connector's `multi_tenant_watcher` flag exists. Document the operational model.

---

## After push: small Core Reflow PRs

The reviewer asked for "small core Reflow PRs only where the connector needs stable API support." Likely PRs to upstream:

1. **PII approve/deny REST endpoint** (currently we have `/api/v1/documents/{id}/pii/approve` and `/pii/deny` — verify these are stable and documented)
2. **Document submit response includes `markdown_url`** in completed status (verify this is the case in upstream main)
3. **Webhook for status changes** (optional — would replace bridge polling with push notifications, much more efficient at scale)

File these as separate small PRs against upstream after the connector is pushed.
