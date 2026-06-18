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

from connector.api import canvas_consent, canvas_oauth, canvas_panorama, canvas_review
from connector.config import settings
from connector.dependencies import _get_redis_pool
from connector.lti import router as lti_router
from connector.workers.canvas_watcher import start_canvas_watcher
from connector.workers.reflow_bridge_worker import start_reflow_bridge

logger = logging.getLogger("connector")

_PANORAMA_JS_PATH = Path(__file__).resolve().parent / "web" / "canvas_review" / "panorama.js"


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
            except asyncio.TimeoutError:
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
        headers={"Cache-Control": "no-cache"},
    )
