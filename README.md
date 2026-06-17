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
conversion pipeline. See [`PORTING_BRIEF.md`](PORTING_BRIEF.md) for the full
extraction plan and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for details
(populated in Phase I).

## Status

Early — being extracted from the working `equalify-reflow` fork that ran an
end-to-end Canvas demo on 2026-06-17. See `PORTING_BRIEF.md` for the
phase-by-phase porting plan and `CHANGELOG.md`.

## Quick start (local dev)

```bash
cp .env.example .env
# Fill in REFLOW_API_BASE_URL, REFLOW_API_KEY, LTI_*, CANVAS_*
./scripts/generate_lti_keys.sh
docker compose up
```

Then `curl http://localhost:8000/health` and visit
`http://localhost:8000/lti/config.json` to grab the JSON that goes into a
Canvas Developer Key.

For dev hot-reload, layer the override:
```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## License

[AGPL-3.0-or-later](LICENSE). Matches upstream Reflow.

## Acknowledgements

Extracted from the [`equalify-reflow`](https://github.com/EqualifyEverything/equalify-reflow)
Canvas-integration fork. Original Canvas implementation: contributors to that fork.
