# reflow-canvas-lti

A Canvas LMS LTI 1.3 connector for [Equalify Reflow](https://github.com/EqualifyEverything/equalify-reflow).
Mirrors the connector pattern of [equalify-reflow-wp](https://github.com/EqualifyEverything/equalify-reflow-wp).

This service launches inside Canvas, watches courses for PDFs, submits them
to the upstream Reflow Core HTTP API for accessibility conversion, and
publishes the resulting accessible HTML back into Canvas as Pages — with
faculty review, dial-badge overlay, and alt-format generation (HTML, ePub,
OCR'd PDF, audio, braille, translation).

## Architecture

```
      Canvas LMS                       Reflow Core (upstream)
            ↕                                  ↑
            ↕ LTI 1.3, OAuth, REST          HTTP API
            ↕                                  │
     reflow-canvas-lti  ───────────────────────┘
     (this repo)
```

The connector owns Canvas integration concerns; Reflow Core owns the document
conversion pipeline. The two services talk over HTTP — the connector imports
no Reflow Core Python code. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the deeper picture and [`docs/REFLOW_API.md`](docs/REFLOW_API.md) for the
exact endpoints the connector consumes.

## Status

Extracted from the working `equalify-reflow` fork that ran an end-to-end
Canvas demo on 2026-06-17. The connector boots cleanly, talks to a local
Reflow Core, and serves the LTI handshake — verified 2026-06-18, see
[`CHANGELOG.md`](CHANGELOG.md). One follow-up depends on a small upstream
core PR (PII approve/deny REST endpoints) — see "Known follow-ups" in
CHANGELOG.

## Features

- **LTI 1.3 launch** — OIDC handshake, signed JWT validation, public JWKS,
  tool-config JSON for Canvas Developer Key paste-in.
- **Per-instructor Canvas OAuth2** — needed because Canvas Cloud's standard
  REST API doesn't accept LTI Advantage service tokens.
- **Canvas file watcher** — scans configured courses, discovers PDFs, submits
  them to Reflow Core. Multi-tenant aware: walks all registered LTI platforms
  when `MULTI_TENANT_WATCHER=true`.
- **Reflow bridge worker** — polls Reflow Core for completion, renders
  Markdown → Canvas-safe HTML, uploads figures, creates/updates a Canvas Page.
- **Faculty Panorama overlay** — Theme-Editor-injected JS bundle that paints
  per-document accessibility dial badges over Canvas's own file UI.
- **PII approval gate** — when Reflow Core flags PII, faculty get a Canvas
  Page-style review screen with approve/deny actions.
- **Alt-format generators** — HTML, plain text, ePub, OCR'd PDF, Amazon Polly
  audio, AI translation.
- **Automated WCAG checks** — axe-core-style checks against the generated HTML
  before publication.
- **Per-course AI API spend cap** — protects the upstream AI budget.

## Quick start (local dev)

```bash
cp .env.example .env
# Fill in REFLOW_API_BASE_URL, REFLOW_API_KEY, LTI_*, CANVAS_*
./scripts/generate_lti_keys.sh
docker compose up
```

Then visit `http://localhost:8000/health` to confirm the connector booted, and
`http://localhost:8000/lti/config.json` to grab the JSON that goes into a
Canvas Developer Key (see [`docs/CANVAS_SETUP.md`](docs/CANVAS_SETUP.md)).

For dev hot-reload, layer the override:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

For an end-to-end smoke against a local Reflow Core, see [`docs/PILOT_RUNBOOK.md`](docs/PILOT_RUNBOOK.md).

## Configuration

All settings live in `.env`. [`.env.example`](.env.example) documents every
field. The minimum to boot:

- `REFLOW_API_BASE_URL` — where Reflow Core is reachable.
- `REFLOW_API_KEY` — bearer key Reflow Core issued for this connector.
- `LTI_ENABLED=true` plus `LTI_ISSUER`, `LTI_CLIENT_ID`, `LTI_DEPLOYMENT_ID`,
  `LTI_PUBLIC_URL` (from your Canvas Developer Key).
- `CANVAS_API_URL` and either `CANVAS_API_TOKEN` (single-tenant) or both
  `CANVAS_OAUTH_CLIENT_ID` and `CANVAS_OAUTH_CLIENT_SECRET` (multi-tenant
  per-instructor OAuth).

## Repo layout

```
connector/
├── main.py              FastAPI entrypoint + worker lifespan
├── config.py            Settings (env-driven, pydantic-settings)
├── dependencies.py      Shared Redis client
├── logging_setup.py     Context-aware logger
├── lti/                 LTI 1.3 handshake, JWKS, platform registry
├── canvas/              Canvas client, OAuth, alt-formats, state
├── api/                 Canvas-facing routers (consent, OAuth, panorama, review)
├── workers/             canvas_watcher + reflow_bridge_worker
└── web/canvas_review/   Front-end (overlay JS, review HTML templates)
docs/                    Operator and architecture docs
scripts/                 Preflight + LTI key generation
keys/                    RSA keypair (gitignored)
```

## License

[AGPL-3.0-or-later](LICENSE). Matches upstream Reflow.

## Acknowledgements

Extracted from the [`equalify-reflow`](https://github.com/EqualifyEverything/equalify-reflow)
Canvas-integration fork. Original Canvas implementation: contributors to that fork.
