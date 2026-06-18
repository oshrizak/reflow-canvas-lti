# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial scaffold extracted from the `equalify-reflow` fork's working Canvas
  LTI integration (validated end-to-end against CSUEB Canvas on 2026-06-17).
  See `PORTING_BRIEF.md` for the phased porting plan.

### Verified
- 2026-06-18 — local end-to-end smoke test against the `equalify-reflow` source
  fork stack: `docker compose up` boots cleanly, `/health` / `/lti/config.json` /
  `/lti/jwks.json` all return well-formed responses, and the connector's
  `ReflowClient.submit_document` + `get_status` successfully drive a PDF
  submission against Reflow Core's `/api/v1/documents/*` endpoints.

### Known follow-ups
- `POST /api/v1/documents/{id}/pii/{approve,deny}` need to be added to upstream
  Reflow Core. The connector's `canvas_panorama.pii_decision` handler calls them
  via `ReflowClient.submit_pii_decision`; today the call returns 404. Filed as a
  small upstream PR per `PORTING_BRIEF.md` "After push: small Core Reflow PRs".
