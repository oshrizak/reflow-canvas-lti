# Architecture

`reflow-canvas-lti` is a Canvas LMS connector that sits between Canvas and the
upstream [Equalify Reflow](https://github.com/EqualifyEverything/equalify-reflow)
document accessibility API. It is intentionally a thin client of Reflow Core —
the heavy lifting (Docling extraction, PII detection, AI agents) stays in core.

```
   Canvas LMS                            Reflow Core (upstream)
   (CSUEB / etc.)                        equalify-reflow on main
        ↕                                            ↑
        ↕ LTI 1.3, OAuth, REST                   HTTP API
        ↕                                            │
  reflow-canvas-lti  ──────────────────────────────┘
  (this repo)
```

## Why split this out

Earlier, the Canvas integration lived inside the `equalify-reflow` fork — every
Canvas import lived next to the conversion pipeline, agents, and PII workers.
The upstream Equalify maintainer asked to split it because keeping Canvas as a
sibling of the conversion pipeline turned Canvas into a first-class concern of
the core service: every core deploy carried Canvas code; every change to core's
service container shape rippled through the Canvas modules. The split moves
Canvas out into its own deployable that depends on the public Reflow Core HTTP
API instead.

The same shape is used by [`equalify-reflow-wp`](https://github.com/EqualifyEverything/equalify-reflow-wp)
(the WordPress connector). Treating Canvas and WP as peer connectors gives core
Reflow a stable seam.

## Component responsibilities

### Connector owns
- **LTI 1.3 launch + OIDC** — `connector/lti/` ports the source fork's working
  implementation. Handles `/lti/login`, `/lti/launch`, JWKS, `/lti/config.json`.
- **Per-instructor Canvas OAuth** — `connector/canvas/user_oauth.py` and
  `connector/api/canvas_oauth.py` implement the Phase-8 flow Canvas Cloud
  requires for general REST access.
- **Canvas REST client** — `connector/canvas/client.py` wraps the Canvas API
  the integration touches (files, pages, modules, discussions, conversations).
- **File watcher** — `connector/workers/canvas_watcher.py` polls watched
  courses on a configurable interval, discovers PDFs, and submits them to
  Reflow Core via `ReflowClient`.
- **Reflow bridge** — `connector/workers/reflow_bridge_worker.py` polls Reflow
  Core for each in-flight job; on completion fetches the markdown, renders it
  to Canvas-safe HTML, uploads figures into a course folder, and creates or
  updates a Canvas Page.
- **Panorama overlay** — `connector/web/canvas_review/panorama.js` is the
  Theme-Editor-injected JS that paints accessibility dials onto Canvas's
  native file listings. `connector/api/canvas_panorama.py` is the server
  side: per-document metadata, alt-format downloads, faculty
  approve/reject/PII actions, WCAG checks.
- **Per-job Canvas-side state** — Redis keys under `eq-pdf:lti:*` and
  `eq-pdf:canvas:*`, namespaced per tenant via `connector/canvas/tenant.py`.
- **Alt-format generators** — `connector/canvas/alt_formats.py` renders
  ePub, OCR'd PDF, plain text, Polly audio, AI translation from the canonical
  accessible HTML.
- **Automated WCAG checks** — `connector/canvas/wcag_checks.py` runs against
  generated HTML before publication.

### Reflow Core (upstream) owns
- Document conversion pipeline (Docling + pydantic-ai agents).
- PII detection (Presidio).
- S3 result storage and presigned URL generation.
- The public document API (`/api/v1/documents/*`) the connector calls.

The connector imports **nothing** from `src.services.*` or `src.agents.*`. The
seam is HTTP. See [REFLOW_API.md](REFLOW_API.md) for the exact endpoints.

## Data flow

A typical happy path:

1. Faculty member uploads a PDF to a Canvas course.
2. `canvas_watcher` discovers it on its next tick (default every 60s) and
   submits the PDF bytes to Reflow Core via
   `POST /api/v1/documents/submit`. Stores a `CanvasJob` in Redis at
   `eq-pdf:canvas:job:{job_id}`.
3. `reflow_bridge_worker` polls `GET /api/v1/documents/{job_id}` every
   `REFLOW_POLL_SECONDS`. On `status=awaiting_approval` (Reflow flagged PII),
   marks the canvas job `awaiting_approval` and waits.
4. Faculty hits the Panorama overlay (or the LTI tool's queue), sees the
   PII gate, reads findings, approves or denies. The handler calls
   `ReflowClient.submit_pii_decision()` which: GETs the doc status to read
   the current `approval_token`, then POSTs to
   `/api/v1/approval/{token}/decision`. See
   [REFLOW_API.md](REFLOW_API.md) for the exact contract.
5. On `status=completed`, the bridge fetches `markdown_url` (presigned S3),
   rewrites figure refs to permanent Canvas file URLs, sanitises HTML,
   creates/updates the Canvas Page, advances the canvas job to
   `awaiting_review`.
6. Faculty reviews the rendered Page in Canvas. `/canvas/panorama/approve/{job_id}`
   runs WCAG checks; if clean (or instructor waivers cover the issues), the
   Page publishes and students see it.

## Redis key shape

```
eq-pdf[:t:{tenant}][:p:{platform_id}]:<suffix>
```

`canvas/tenant.py::tk()` builds these. `tenant` defaults to `default` (legacy,
unprefixed). `platform_id` is added only when the call passes `platform=`,
sandboxing per-Canvas-instance data when one connector deployment serves many
institutions.

Important suffixes:

| Suffix | Purpose |
|---|---|
| `canvas:job:{id}` | A converted-document job's connector-side state |
| `canvas:file:{course_id}:{file_id}` | Watcher discovery cache + Reflow job pointer |
| `canvas:state:{nonce}` | LTI OIDC state, single-use |
| `canvas:session:{session_id}` | Faculty LTI session payload |
| `lti:platform:{platform_id}` | `PlatformInstall` records |
| `lti:platforms` | Set of known platforms |
| `lti:course:{course_id}:owner` | Whose OAuth token authorises scans of the course |
| `canvas:user_oauth:*` | Per-instructor OAuth access + refresh tokens |
| `canvas:audit:*` | Append-only approval/decision audit log |

## Alt-format pipeline

The canonical accessible representation is the HTML body produced by
`canvas/markdown_to_html.render` from Reflow's markdown. Every other
alt-format derives from that same RenderedPage:

- **HTML / HTML with math** — `alt_formats.html_full_document`. Auto-
  detects LaTeX delimiters and `\ce{}`/`\pu{}` mhchem markup via
  `alt_formats.has_math_content`; on a hit, the rendered page loads
  MathJax with the mhchem extension. Inline-`$...$` regex is tightened
  so `$5 and another $7` prose doesn't trigger MathJax.
- **Plain text / Markdown** — tag-strip and passthrough.
- **ePub** — `ebooklib` (EPUB3).
- **Searchable PDF (auto-route)** —
  `canvas/alt_formats.pdf_has_text_layer` decides. Image-only scans
  go through `ocrmypdf`. Born-digital PDFs go through WeasyPrint with
  `pdf_variant='pdf/ua-1'` for a real Tagged PDF (structure tree,
  reading order, alt text). Math in the source HTML is pre-rendered
  to inline SVG via `canvas/math_render` because WeasyPrint doesn't
  run JavaScript.
- **Audio MP3** — Amazon Polly neural TTS, chunked at ~2800 chars per
  Polly call.
- **Translate** — Anthropic Claude (Sonnet 4.5) via the Messages API.
  Prompt explicitly preserves LaTeX and `\ce{}` markup verbatim so
  equations don't get translated into the target language's prose.
- **Braille (BRF)** — liblouis. Math-bearing documents route to
  Nemeth code (`nemeth.ctb`); prose to `en-us-g2.ctb`. Strips LaTeX
  delimiters before handoff so Nemeth transcribes the symbols, not the
  fences.

`canvas/pdf_figures.py` and the `/canvas/panorama/alt/{job}/figures/{ref}`
route give every rendered surface clean figure bytes pulled from the
source PDF (PyMuPDF embedded-raster extraction). Reflow's S3 PNGs
(which carry a vision-pipeline grid overlay) are the last-resort
fallback for vector figures the connector can't extract directly.

## Security model

State-changing requests need to clear, in order:

1. **LTI session cookie** (`reflow_lti_session`) — set by `/lti/launch`,
   carries the user id, course id, and roles.
2. **CSRF token** (`X-CSRF-Token` header, fetched via
   `/canvas/panorama/csrf`) — HMAC over the session id with
   `CSRF_SECRET_KEY`.
3. **Trusted-origin gate** — request's Origin (or Referer base) must be
   in `CANVAS_ALLOWED_ORIGINS` or match `CANVAS_ALLOWED_ORIGIN_REGEX`.
4. **Rate limit** — per `(endpoint, user_id)`. Redis-backed fixed
   window. See [OPERATIONS.md](../OPERATIONS.md#rate-limiting) for the
   table.
5. **Role check** — Instructor / TA / ContentDeveloper / Admin for
   approve/reject/edit/PII paths.
6. **Course check** — the LTI session's `course_id` must match the
   job's `canvas_course_id`.

Instructor OAuth tokens are encrypted at rest with AES-GCM
(`canvas/privacy.py`). The encryption key derives from
`TOKEN_ENCRYPTION_KEY`; if unset it falls back to a derivation of
`CSRF_SECRET_KEY`; if both are unset, a hardcoded constant — the
connector logs `CRITICAL` once per process in that mode, plus a
boot-time audit line. Production deployments set both keys.

The publication gate (`REQUIRE_WCAG_GATE=true`) makes WCAG-error
findings and the 4-item reviewer checklist a hard prerequisite for
the approve handler. When off, both run but failures are advisory.

## Multi-tenant model

One connector deployment can serve many Canvas instances at once. Two layers
of isolation:

- **Deployment tenant** — process-wide, set via `CANVAS_TENANT`. Picks the
  base Redis prefix. Used when one institution wants several logical
  deployments inside the same Redis (e.g. dev / staging / prod on shared
  infra).
- **LTI platform** — per call, set via `tk(suffix, platform=...)`. Sandboxes
  per-Canvas-instance data. Two Canvas instances managed by the same
  connector cannot read each other's per-platform records.

When `MULTI_TENANT_WATCHER=true`, the watcher iterates every registered
`PlatformInstall` and walks the courses associated with it via
`lti:platform:{pid}:courses`. When false, it just walks `CANVAS_WATCHED_COURSES`
under the single configured Canvas API token.

## Lifespan

`connector/main.py` runs both workers as background `asyncio.Task`s under the
FastAPI lifespan, sharing the Redis connection pool with request-time
handlers. Workers are gated on `LTI_ENABLED` so a connector running purely
behind a placement-less Developer Key (rare) doesn't burn cycles. On
shutdown the lifespan signals an `asyncio.Event` and waits up to 10 s
before cancelling.

## What lives where

```
connector/
├── main.py                 # FastAPI app + worker lifespan
├── config.py               # Settings (env-driven, pydantic-settings)
├── dependencies.py         # Singleton Redis pool + get_redis_client
├── logging_setup.py        # Context-aware JSON/text logging
├── lti/                    # LTI 1.3
│   ├── routes.py           # /lti/login, /launch, /jwks, /config.json
│   ├── jwt_validation.py   # Validate Canvas-issued launch JWTs
│   ├── keys.py             # RSA keypair + JWKS doc
│   ├── platform.py         # PlatformInstall, URL derivation
│   ├── platform_store.py   # Per-platform Redis upsert + index
│   ├── session.py          # OIDC state + post-launch session
│   └── config.py           # Thin view over global Settings
├── canvas/
│   ├── client.py           # Canvas REST API wrapper
│   ├── oauth.py            # LTI Advantage service-token mint
│   ├── user_oauth.py       # Per-instructor Canvas OAuth
│   ├── reflow_client.py    # Reflow Core HTTP client (the seam)
│   ├── state.py            # CanvasJob storage
│   ├── tenant.py           # Redis key namespacer (tk)
│   ├── alt_formats.py      # ePub, audio, OCR PDF, translation
│   ├── markdown_to_html.py # Reflow MD → Canvas-safe HTML
│   ├── sanitize.py         # HTML allowlist for instructor edits
│   ├── wcag_checks.py      # Axe-core-style checks
│   ├── panorama.py         # Overlay dial data shapes
│   ├── signals.py          # Per-document conversion-quality signals
│   ├── privacy.py          # PII redaction + token encryption
│   ├── spend_cap.py        # Per-course AI spend cap
│   ├── audit.py            # Append-only audit log
│   └── errors.py           # CanvasApiError
├── api/
│   ├── canvas_consent.py   # Faculty disclaimer + opt-in
│   ├── canvas_oauth.py     # Canvas user-OAuth authorise + callback
│   ├── canvas_panorama.py  # Overlay JSON, dial badges, alt-formats, PII gate
│   ├── canvas_review.py    # Faculty review queue + per-doc page
│   └── _pii_approval_page.py  # HTML renderer for PII approval gate
├── canvas/
│   ├── pdf_figures.py      # Extract clean figure rasters from source PDF
│   ├── math_render.py      # LaTeX / mhchem → inline SVG for Tagged PDF
│   ├── verapdf_audit.py    # PDF/UA-1 audit subprocess wrapper
│   └── …                   # (other modules listed earlier in this file)
├── utils/
│   ├── rate_limit.py       # Redis-backed per-(endpoint, user) limiter
│   └── retry_helpers.py    # Exponential backoff for upstream calls
├── tools/                  # Operator CLIs (python -m connector.tools.*)
│   ├── generate_keys.py    # TOKEN_ENCRYPTION_KEY + CSRF_SECRET_KEY mint
│   ├── reprocess_figures.py # Backfill PDF-extracted figures for old jobs
│   └── …
├── workers/
│   ├── canvas_watcher.py   # Course poll + Reflow submission
│   └── reflow_bridge_worker.py  # Reflow status poll + Canvas Page write
└── web/canvas_review/
    ├── panorama.js         # Theme-Editor overlay bundle (dial, modal, gate UI)
    ├── dashboard.html      # Instructor dashboard template
    ├── index.html          # Review queue template
    └── one.html            # Per-document review template (side-by-side)
```

## Tests

```
tests/
├── unit/           Pure-logic tests (markdown rendering, math detection,
│                   PDF figure matching, rate-limit math, structure-tree
│                   walking, PDF text-layer detection, ...)
└── integration/    End-to-end with fakeredis + real FastAPI app +
                    real LTI session + CSRF + rate-limit plumbing. Outbound
                    HTTP (Reflow Core, Canvas) mocked with respx.
```

The integration suite specifically covers the wire contract for the
PII decision flow and the approve-and-publish flow — both broke
multiple times during the CSU East Bay pilot at boundaries the unit
suite couldn't see (wrong upstream URLs, missing CSRF token, missing
trusted-origin headers).
