# reflow-canvas-lti

A Canvas LMS LTI 1.3 connector for [Equalify Reflow](https://github.com/EqualifyEverything/equalify-reflow).
The connector launches inside Canvas, watches courses for new PDFs, sends
them to the upstream Reflow Core HTTP API for accessibility conversion,
and publishes the resulting accessible HTML back into Canvas as Pages —
with a faculty review workflow, a dial-badge overlay over Canvas's own
file UI, and a full alt-format catalogue (accessible HTML, tagged PDF,
ePub, Braille, audio, translation, plain text, Markdown).

```
   Canvas LMS                        Reflow Core (upstream)
        ↕                                     ↑
        ↕ LTI 1.3, OAuth, REST            HTTP API
        ↕                                     │
  reflow-canvas-lti  ───────────────────────┘
  (this repo)
```

The connector owns the Canvas-side experience and state; Reflow Core owns
the document pipeline. The two services talk over HTTP — the connector
imports zero Reflow Core Python. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture and
[`docs/REFLOW_API.md`](docs/REFLOW_API.md) for the exact endpoints the
connector consumes.

## Who this is for

- **Faculty** — upload a PDF, get an accessible Canvas Page in minutes,
  review before students see it, fix anything with an in-browser editor,
  decide what counts as a waivable accessibility finding.
- **Students** — open the row's dial, get the accessible version in the
  format that works for them (HTML, Braille, audio, translated to their
  L1, etc.).
- **Operators / Canvas admins** — runbook-driven deployment, see
  [`OPERATIONS.md`](OPERATIONS.md).

## Features

### Faculty surfaces

- **Accessibility dial on every PDF row** in Canvas Files, with two
  honest numbers: PDF/UA-1 score (veraPDF) on the original PDF, and a
  WCAG structural-check score on the generated HTML. These measure
  different things and the UI labels them as such — there is no
  before/after framing.
- **Pending-scan marker** on rows the watcher hasn't picked up yet, so
  faculty isn't left wondering whether processing started.
- **In-modal alt-format menu** grouped by purpose: Read, Listen &
  translate, Document formats. 10 generated formats; the original
  source PDF stays in Canvas Files where faculty uploaded it.
- **Per-document review screen** (`/canvas/review/{job}`) — side-by-side
  live PDF preview + accessible HTML (or live Canvas Page once
  published). Approve, reject, pull-back-to-draft, unpublish.
- **Inline HTML editor** for the accessible version — fix table semantics,
  alt text, headings, etc.; saves become the source of truth for every
  downstream format.
- **PII review queue** surfaced in the LTI tool ("Accessible Documents")
  with badges for "PII review" vs "Accessibility review". PII gates
  block the rest of the pipeline.
- **WCAG publication gate** (opt-in via `REQUIRE_WCAG_GATE=true`) — the
  approval modal renders a 4-item visual-inspection checklist plus
  per-rule waivers for any automated WCAG errors. Faculty can't publish
  with unwaived errors when the gate is on; nothing changes when it's off.

### Alt-formats

| Format | Tool | Notes |
|---|---|---|
| Accessible HTML | mistune + sanitize | Canonical source for every other format |
| HTML with math | + MathJax (mhchem extension) | LaTeX and chemistry markup render to MathML at view time |
| Plain text | tag-strip | UTF-8, no markup |
| Markdown | passthrough from Reflow | The canonical Reflow output |
| Tagged PDF | WeasyPrint (pdf/ua-1) | Born-digital input: produces a real structure tree (`StructTreeRoot`, `MarkInfo`, language metadata). Image-only input: falls back to ocrmypdf. Math renders as inline SVG via matplotlib's mathtext so equations are visible in the PDF and the LaTeX source is preserved as the figure `alt` for screen readers. |
| Searchable PDF | ocrmypdf | Image-only scans get an OCR text layer |
| ePub | ebooklib | EPUB3 |
| Audio (MP3) | Amazon Polly | Requires `AWS_DEFAULT_REGION` + IAM credentials with `polly:SynthesizeSpeech` |
| Translate | Anthropic Claude (Sonnet 4.5) | Requires `ANTHROPIC_API_KEY`. Prompt explicitly preserves LaTeX math and `\ce{}` chemistry markup verbatim. |
| Braille (BRF) | liblouis | Auto-routes to Nemeth code (math/chemistry) or en-us-g2 (prose) based on document content |

### Behind the scenes

- **LTI 1.3 launch** — OIDC handshake, signed JWT validation, public
  JWKS, tool-config JSON for Canvas Developer Key paste-in.
- **Per-instructor Canvas OAuth2** — required because Canvas Cloud's
  general `/api/v1/*` REST endpoints don't accept LTI Advantage service
  tokens. Tokens are encrypted at rest with AES-GCM (see Security
  below).
- **Canvas file watcher** — discovers new PDFs and submits them to Reflow
  Core. Configurable per-course or multi-tenant across every registered
  LTI platform.
- **Reflow bridge worker** — polls Reflow Core for completion, renders
  Markdown → Canvas-safe HTML, extracts figures directly from the source
  PDF via PyMuPDF (cleaner than Reflow's S3 copies which carry a
  vision-model overlay), uploads figures into a course folder, creates
  or updates a Canvas Page.
- **veraPDF integration** — every source PDF audited against PDF/UA-1
  on submission; failed rules surfaced inline in the alt-format modal.
- **PII gate** — when Reflow Core flags PII (Microsoft Presidio
  upstream), faculty see a CSRF-protected approval form in either the
  LTI tool's queue or the panorama overlay's modal.

### Security & ops

- **OAuth tokens encrypted at rest** with AES-GCM (`connector/canvas/privacy.py`).
  Key derivation falls back through `TOKEN_ENCRYPTION_KEY` →
  `CSRF_SECRET_KEY` → a constant; the connector logs `CRITICAL` once
  per process on the constant fallback so it's impossible to miss in
  prod logs.
- **CSRF on every state-changing POST.** No exceptions; the panorama
  overlay fetches a token via `/canvas/panorama/csrf` on load.
- **Rate limiting per `(endpoint, user_id)`** on every state-changing
  POST. 30/min for approve/reject; 60/min for the auto-save editor;
  10/min for PII decisions; 5/min for bulk approve. See
  [`OPERATIONS.md`](OPERATIONS.md#rate-limiting).
- **Trusted-origin allowlist** (`CANVAS_ALLOWED_ORIGINS`) blocks cross-
  origin state changes from anything that isn't your Canvas host.
- **Append-only audit log** of every approve / reject / unpublish / PII
  decision, with retention controls.
- **Startup secrets audit** logs `CRITICAL` for every production-required
  secret that's unset. Doesn't block boot; impossible to miss.
- **Redis persistence** (AOF + RDB) on a named volume; container restart
  no longer wipes faculty consent records or the audit log. Off-host
  backup via `scripts/backup-redis.sh` (cron-friendly, optional S3
  upload).
- **Integration tests** covering the LTI session + CSRF + rate-limit
  pipeline for the PII decision flow and the publish-approval flow.

## Quick start (local dev)

```bash
cp .env.example .env
# Fill in REFLOW_API_BASE_URL, REFLOW_API_KEY, LTI_*, CANVAS_*.
# Generate the secrets:
docker compose run --rm connector python -m connector.tools.generate_keys >> .env
./scripts/generate_lti_keys.sh
docker compose up
```

Visit `http://localhost:8000/health` for liveness and
`http://localhost:8000/lti/config.json` for the JSON to paste into a
Canvas Developer Key (full walkthrough:
[`docs/CANVAS_SETUP.md`](docs/CANVAS_SETUP.md)).

For dev hot-reload, layer the override:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

For an end-to-end smoke test against a local Reflow Core, see
[`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md).

## Configuration

Every setting is documented in [`.env.example`](.env.example). Minimum
to boot:

- `REFLOW_API_BASE_URL` + `REFLOW_API_KEY` — where Reflow Core is
  reachable and the X-API-Key it expects.
- `LTI_ENABLED=true` plus `LTI_ISSUER`, `LTI_CLIENT_ID`,
  `LTI_DEPLOYMENT_ID`, `LTI_PUBLIC_URL` (from your Canvas Developer
  Key).
- `CANVAS_API_URL` and `CANVAS_OAUTH_CLIENT_ID` +
  `CANVAS_OAUTH_CLIENT_SECRET` (multi-tenant per-instructor OAuth).
- `CANVAS_ALLOWED_ORIGINS` — every Canvas host you'll serve from.

Required-for-production but optional-for-dev:

- `TOKEN_ENCRYPTION_KEY`, `CSRF_SECRET_KEY` — generated via
  `python -m connector.tools.generate_keys`.
- `REQUIRE_WCAG_GATE=true` — enforces the publication gate.
- `ANTHROPIC_API_KEY` — only if you're exposing Translate.
- `AWS_DEFAULT_REGION` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
  — only if you're exposing Audio MP3 (Polly).

The connector logs a `CRITICAL` audit line on startup for every
production-required secret that's unset. See
[`OPERATIONS.md`](OPERATIONS.md#secrets-checklist-before-first-launch)
for the complete checklist.

## Repository layout

```
connector/
├── main.py              FastAPI app + worker lifespan + startup secrets audit
├── config.py            Settings (env-driven, pydantic-settings)
├── dependencies.py      Shared Redis pool
├── lti/                 LTI 1.3 handshake, JWKS, platform registry
├── canvas/              Canvas client, OAuth, alt-formats, state, privacy,
│                        pdf_figures, math_render, verapdf_audit
├── api/                 Canvas-facing routers (consent, OAuth, panorama, review)
├── workers/             canvas_watcher + reflow_bridge_worker
├── tools/               Operator CLIs (generate_keys, reprocess_figures, …)
├── utils/               Cross-cutting helpers (rate_limit, retry_helpers)
└── web/canvas_review/   Front-end (overlay JS, review HTML templates)

docs/                    Architecture + ops + Reflow API contract docs
scripts/                 LTI key generation, Redis backup
tests/
├── unit/                Pure-logic tests (63)
└── integration/         End-to-end LTI session + CSRF + rate-limit (6)
```

## Documentation index

- [`OPERATIONS.md`](OPERATIONS.md) — operator runbook: secrets, backups,
  rate limits, breakage modes, recovery procedures.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component map, data
  flow, Redis key shape, multi-tenant model.
- [`docs/REFLOW_API.md`](docs/REFLOW_API.md) — the HTTP contract with
  upstream Reflow Core.
- [`docs/CANVAS_SETUP.md`](docs/CANVAS_SETUP.md) — Canvas admin
  walkthrough: Developer Keys, scopes, placements.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — image, env, networking, health.
- [`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md) — end-to-end smoke
  test and the first-week failure modes.
- [`CHANGELOG.md`](CHANGELOG.md) — released changes.

## License

[AGPL-3.0-or-later](LICENSE). Matches upstream Reflow.

## Acknowledgements

Extracted from the [`equalify-reflow`](https://github.com/EqualifyEverything/equalify-reflow)
Canvas-integration fork that ran the first CSU East Bay pilot. The
original Canvas implementation work was done by the contributors to
that fork.
