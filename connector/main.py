"""Connector entrypoint.

Boots a FastAPI app that exposes a ``/health`` probe, the LTI 1.3
handshake endpoints, and the Canvas API routers (consent, OAuth, panorama,
review). Background workers (canvas_watcher + reflow_bridge_worker) are
wired in Phase F.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from connector.api import canvas_consent, canvas_oauth, canvas_panorama, canvas_review
from connector.config import settings
from connector.lti import router as lti_router

logger = logging.getLogger("connector")


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
    yield
    logger.info("connector shutting down")


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
