"""Connector entrypoint.

Boots a FastAPI app that exposes a ``/health`` probe, the LTI 1.3
handshake endpoints, and (in later phases) the Canvas API + Panorama
routers, plus the ``canvas_watcher`` and ``reflow_bridge_worker``
background tasks.

This Phase C version mounts the LTI router. Canvas API routers and
background workers are wired in Phases D-F.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

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


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe used by Docker and orchestrators."""
    return {"status": "ok"}
