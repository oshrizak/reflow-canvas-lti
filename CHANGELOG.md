# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **PDF figure extraction from the source PDF** (`connector/canvas/pdf_figures.py`,
  `connector/tools/reprocess_figures.py`). Bypasses Reflow's vision-pipeline
  S3 PNGs (which carry a tile/segmentation grid overlay) and pulls clean
  embedded rasters directly via PyMuPDF. Matching uses Reflow's per-figure
  page number + reading-order rank within the page. Vector figures fall back
  to Reflow's S3 copy. CLI script backfills existing jobs.
- **Same-origin figure proxy** at `/canvas/panorama/alt/{job}/figures/{ref}`
  serves PDF-extracted bytes when the bridge hasn't uploaded to Canvas Files
  yet, keeping rendered HTML self-contained even mid-rollout.
- **Tagged PDF output** (PDF/UA-1) via WeasyPrint for born-digital inputs.
  Real structure tree (`StructTreeRoot`, `MarkInfo /Marked true`,
  `ViewerPreferences /DisplayDocTitle`), structure elements for H1/H2/P/L/LI/Table/TR/TD/Figure.
  Auto-routes between WeasyPrint (text PDFs) and ocrmypdf (scans) based on
  `pdf_has_text_layer`.
- **Server-side LaTeX → inline SVG math rendering** (`connector/canvas/math_render.py`)
  so the Tagged PDF carries rendered equations rather than literal `$E=mc^2$` text.
  Uses matplotlib's mathtext (LaTeX subset, no system TeX install needed). LaTeX
  source rides along as `<img alt>` so screen readers consuming the PDF speak
  it correctly.
- **mhchem chemistry preprocessing** — `\ce{H2O}` → `H_{2}O`, `\ce{... -> ...}` →
  `\rightarrow`, then handed to mathtext. Handles the common subset; complex
  reactions fall through as text.
- **Math/chemistry auto-detection in HTML output.** `html_full_document` enables
  MathJax (with the mhchem extension) when LaTeX delimiters or `\ce{}` are
  present in the rendered body. Inline-`$...$` regex tightened to skip prose
  with money values.
- **Math-aware Braille** — `render_braille_brf` routes math-bearing documents
  through liblouis's Nemeth code (`nemeth.ctb`) instead of `en-us-g2.ctb`.
  LaTeX delimiters stripped first so Nemeth transcribes the symbols, not
  the fence characters.
- **PII review surfaced in the LTI tool's queue.** `awaiting_approval` jobs
  now join the per-course pending set alongside `awaiting_review` jobs;
  index.html renders distinct badges (PII review vs Accessibility review) and
  routes PII rows to a dedicated `/canvas/review/{job}/pii` page. CSRF token
  embedded server-side so the form actually submits.
- **WCAG publication gate UI.** The approve handler returns structured 409s
  (`error: wcag_gate_blocked` / `checklist_incomplete`) when `REQUIRE_WCAG_GATE=true`;
  the panorama overlay catches them and swaps the action row for an inline
  gate panel — 4-item checklist plus per-rule waiver checkboxes. Same POST
  resubmits with `waivers` and `checklist` populated.
- **Live PDF + Canvas Page proxies** on the per-document review screen
  (`/canvas/review/{job}/pdf` and `/canvas/review/{job}/canvas-page`) so faculty
  see the original PDF and the live published Canvas Page side-by-side without
  the cross-origin iframe block.
- **Pending-scan marker on Files page rows** for PDFs the watcher hasn't
  picked up yet, with a tooltip explaining the ~60s window.
- **Audio MP3 + Translate alt-formats** properly wired: Audio uses Amazon Polly
  (`polly:SynthesizeSpeech`); Translate uses Anthropic Claude Sonnet 4.5 directly
  (no pydantic-ai / model-tier indirection). Both surface clean 503s when
  credentials are missing.
- **OAuth tokens encrypted at rest** with AES-GCM. Key derivation: explicit
  `TOKEN_ENCRYPTION_KEY` → `CSRF_SECRET_KEY` derivation → hardcoded constant.
  The constant fallback now logs `CRITICAL` once per process with the keygen
  command.
- **Startup secrets audit** in `main.lifespan` logs `CRITICAL` per missing
  production-required secret (encryption key, CSRF key, Reflow API key
  placeholder). Doesn't block boot.
- **`connector/tools/generate_keys`** mints fresh `TOKEN_ENCRYPTION_KEY` and
  `CSRF_SECRET_KEY` (`secrets.token_urlsafe(32)` each).
- **Rate limiting** (`connector/utils/rate_limit.py`) on every state-changing
  POST/PUT, scoped per `(endpoint, user_id)`. 30/min for approve/reject/etc.,
  60/min for editor saves, 10/min for PII decisions, 5/min for bulk approve.
  Redis fixed-window counter with auto-expiry.
- **Redis persistence** turned on in `docker-compose.yml` — AOF
  (`appendfsync everysec`) + RDB snapshots, named `redis-data` volume,
  `restart: unless-stopped`. Container lifecycle no longer wipes faculty
  consent records, the audit log, or OAuth tokens.
- **Off-host backup script** (`scripts/backup-redis.sh`) — `BGSAVE` → poll
  `LASTSAVE` → `docker cp` → optional S3 upload via `aws` CLI →
  retention-window prune. Cron-friendly.
- **Integration test suite** (`tests/integration/`) using `fakeredis` + the
  real FastAPI app + LTI session + CSRF + rate-limit plumbing. Covers the
  PII decision wire contract, the approve flow's state persistence, and the
  rate-limiter threshold.
- **`OPERATIONS.md` runbook** — secrets checklist, Redis backup/restore,
  rate-limit table, common breakage modes from the CSU East Bay pilot,
  key rotation consequences.

### Fixed

- **PII decision endpoint URL.** The connector used to POST to
  `/api/v1/documents/{job}/pii/approve`, which Reflow Core never had —
  Core returned 405 Method Not Allowed, the connector wrapped that as 502,
  and the panorama overlay's `fetch` came back as "Failed to fetch". Core's
  actual PII gate uses an approval-token model:
  `POST /api/v1/approval/{token}/decision`, where the token comes from the
  job's status payload. The connector now does that. Closes the
  "PII approve/deny pending upstream" follow-up.
- **`get_page` slug normalization.** Was 404'ing because the connector
  stored the full Canvas Page URL as `canvas_page_url` and `get_page`
  interpolated it directly into the API path. Now uses `_page_ref` like
  every sibling page method (publish_page, update_page, delete_page).
- **Approve/reject/PII-gate routes used the bare `CanvasClient()` constructor,**
  which fell back to env-token and 500'd on Canvas Cloud. All now use
  `_canvas_client_for_job` (instructor OAuth token).
- **WeasyPrint `pdf_variant` kwarg silently dropped** when passed inside
  an `options=` dict (only logged a one-line warning). The Tagged PDF had
  no `StructTreeRoot`. Pass as a direct kwarg — Acrobat's Accessibility
  panel now reads the tree.
- **Standalone images mapped to `Figure` inside `P`** in the tagged-PDF
  structure tree because CommonMark wraps any `![alt](src)`-only line in a
  paragraph. `markdown_to_html.render` promotes those to `<figure>` so
  Figure becomes a sibling of P, per PDF/UA-1.
- **`_pii_approval_page` form POSTed without the CSRF token** the decision
  endpoint requires. Token is now embedded server-side.
- **Score labelling.** Dial and modal report stopped framing the
  PDF/UA score (veraPDF, original PDF) and the WCAG structural score
  (generated HTML) as a before/after — they measure two different
  documents with two different tools and aren't directly comparable.
  Labels and tooltips updated; numbers themselves unchanged.

### Verified

- **2026-06-19** — end-to-end against CSU East Bay Canvas: LTI launch,
  OAuth consent, PDF discovery, Reflow Core submission, Canvas Page write,
  PII decision via the real approval-token endpoint, Tagged PDF with
  veraPDF-confirmed structure tree, rate limiting fires at the configured
  threshold under load, secrets audit fires CRITICAL when keys are missing.
- **CI**: 69 tests passing (63 unit + 6 integration). Ruff + mypy clean
  across 52 source files.

## [0.1.0] — 2026-06-18

### Added

- Initial scaffold extracted from the `equalify-reflow` fork's working
  Canvas LTI integration (validated end-to-end against CSU East Bay
  Canvas on 2026-06-17). See `PORTING_BRIEF.md` for the phased porting
  plan.

### Verified

- Local end-to-end smoke test against the source fork stack:
  `docker compose up` boots cleanly, `/health`, `/lti/config.json`, and
  `/lti/jwks.json` return well-formed responses, and the connector's
  `ReflowClient.submit_document` + `get_status` successfully drive a PDF
  submission.
