"""Connector entrypoint.

Boots a FastAPI app that exposes:

  * ``/health`` — liveness probe
  * LTI 1.3 handshake endpoints (``/lti/*``)
  * Canvas API routers (``/canvas/consent``, ``/canvas/oauth``,
    ``/canvas/panorama``, ``/canvas/review``)

And starts two background tasks under the lifespan:

  * ``canvas_watcher`` — polls Canvas courses, discovers files,
    submits them to Reflow Core.
  * ``reflow_bridge_worker`` — polls Reflow Core for completion and
    materialises results into Canvas Pages.

Both workers are no-ops when ``LTI_ENABLED=false`` (they shut down
cleanly on the shutdown event).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from connector.api import canvas_consent, canvas_oauth, canvas_panorama, canvas_review
from connector.config import settings
from connector.dependencies import _get_redis_pool
from connector.lti import router as lti_router
from connector.workers.canvas_watcher import start_canvas_watcher
from connector.workers.reflow_bridge_worker import start_reflow_bridge

logger = logging.getLogger("connector")

_PANORAMA_JS_PATH = Path(__file__).resolve().parent / "web" / "canvas_review" / "panorama.js"


def _audit_startup_secrets() -> None:
    """Log a CRITICAL for every production-required secret that's unset.

    These don't block startup — a dev environment can run fine with
    placeholders — but production deployments should grep their startup
    logs for ``CRITICAL`` after every release.
    """
    import os

    findings: list[str] = []

    # OAuth token encryption — the most directly exploitable miss.
    if not os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip() and not os.environ.get("CSRF_SECRET_KEY", "").strip():
        findings.append(
            "TOKEN_ENCRYPTION_KEY (and CSRF_SECRET_KEY fallback) UNSET — "
            "instructor OAuth tokens encrypted with a hardcoded key; anyone with "
            "this source can decrypt a Redis dump. Generate with "
            "`python -m connector.tools.generate_keys` and set in .env."
        )

    # CSRF token signing — separate but related.
    if not os.environ.get("CSRF_SECRET_KEY", "").strip():
        findings.append(
            "CSRF_SECRET_KEY UNSET — CSRF tokens are signed with a derivation "
            "of the LTI keypair fingerprint, stable but not a secret. Set in .env."
        )

    # Reflow Core auth — every doc submission depends on this.
    raw_key = os.environ.get("REFLOW_API_KEY", "").strip()
    if not raw_key or raw_key == "your-secret-key-here":
        findings.append(
            "REFLOW_API_KEY UNSET or still the placeholder. Reflow Core will "
            "401 every document submission. Set in .env."
        )

    if not findings:
        logger.info("startup secrets audit: OK (no missing production secrets)")
        return

    for f in findings:
        logger.critical("startup secrets audit: %s", f)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "connector starting up — environment=%s reflow_api_base_url=%s lti_enabled=%s",
        settings.environment,
        settings.reflow_api_base_url,
        settings.lti_enabled,
    )

    # Startup secrets audit — CRITICAL if anything that's required for
    # production is unset. Doesn't block boot; an operator can still
    # bring the connector up in dev with placeholders. The logs make
    # it impossible to miss when reviewing a fresh deploy.
    _audit_startup_secrets()

    # Use the shared connection pool the request-time dependency uses, so
    # workers and HTTP handlers share Redis connections instead of
    # opening a second pool.
    import redis.asyncio as redis_module

    redis_client = redis_module.Redis(connection_pool=_get_redis_pool())
    shutdown_event = asyncio.Event()

    watcher_task: asyncio.Task[None] | None = None
    bridge_task: asyncio.Task[None] | None = None
    if settings.lti_enabled:
        watcher_task = asyncio.create_task(
            start_canvas_watcher(redis_client, shutdown_event=shutdown_event),
            name="canvas_watcher",
        )
        bridge_task = asyncio.create_task(
            start_reflow_bridge(redis_client, shutdown_event=shutdown_event),
            name="reflow_bridge",
        )
        logger.info("started background workers (watcher + bridge)")
    else:
        logger.info("LTI disabled — workers not started")

    try:
        yield
    finally:
        logger.info("connector shutting down — signalling workers")
        shutdown_event.set()
        # Drain any running worker tasks so cancellation is clean.
        pending = [t for t in (watcher_task, bridge_task) if t is not None]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10.0)
            except TimeoutError:
                logger.warning("workers did not exit within 10s — cancelling forcefully")
                for t in pending:
                    t.cancel()


app = FastAPI(
    title="Reflow Canvas LTI Connector",
    description=(
        "Canvas LTI 1.3 connector bridging Canvas LMS to the upstream Reflow Core "
        "accessibility API. See PORTING_BRIEF.md for the architecture."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Canvas embeds the panorama bundle inside an iframe served from the
# institutional Canvas origin (e.g. csueb.instructure.com) and makes
# cross-origin fetches from there to the connector. Without explicit
# CORS allow-origin headers the browser blocks every fetch — see the
# /canvas/panorama/csrf, /canvas/consent/status, etc. failures during
# CSUEB testing on 2026-06-18. Operators configure the allowlist via
# CANVAS_ALLOWED_ORIGINS (comma-separated) and optionally
# CANVAS_ALLOWED_ORIGIN_REGEX for wildcard matches.
_origins = [
    o.strip()
    for o in (settings.canvas_allowed_origins or "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=(settings.canvas_allowed_origin_regex or None),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(lti_router)
app.include_router(canvas_consent.router)
app.include_router(canvas_oauth.router)
app.include_router(canvas_panorama.router)
app.include_router(canvas_review.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by Docker and orchestrators."""
    return {"status": "ok"}


@app.get("/panorama.js", include_in_schema=False)
async def panorama_js_root() -> Response:
    """Serve the Theme-Editor JS bundle from the site root.

    Canvas's Theme Editor injects ``<script src="/panorama.js">`` on every
    page; the script lives under the connector's own origin (via
    ``LTI_PUBLIC_URL``) and decides per-page whether to render the
    accessibility overlay. Mirrored at ``/lti/panorama.js`` for the
    legacy loader.
    """
    if not _PANORAMA_JS_PATH.exists():
        return Response(status_code=404, content="// panorama.js bundle not found")
    return Response(
        content=_PANORAMA_JS_PATH.read_bytes(),
        media_type="application/javascript",
        headers={
            # Strong no-cache for browsers, edge proxies, and CDNs.
            # Cloudflare in particular ignores a lone ``no-cache`` directive
            # and applies its own ``max-age=14400`` to JS responses by
            # default; explicit ``no-store`` plus the CF-specific override
            # below stop it from caching at the edge.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "CDN-Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
