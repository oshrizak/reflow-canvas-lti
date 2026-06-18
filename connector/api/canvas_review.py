"""Review endpoints for the Canvas integration.

Mounted at ``/canvas/review``. The LTI launch redirects faculty here
with a valid session cookie; the endpoints below read identity from
Redis-backed session state and operate on Canvas Pages on the faculty's
behalf.

These endpoints intentionally avoid the X-API-Key middleware: faculty
identity is proven by the LTI session cookie, not an API key. The router
is exempted in ``connector.main`` when LTI is enabled.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from redis.asyncio import Redis

from ..canvas.client import CanvasClient
from ..canvas.state import get_job, list_pending, put_job
from ..dependencies import get_redis_client
from ..lti.routes import SESSION_COOKIE
from ..lti.session import SessionPayload, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/canvas/review", tags=["canvas-review"])

_TEMPLATE_DIR = Path(__file__).parent.parent / "web" / "canvas_review"


async def _require_session(
    redis: Redis,
    cookie: str | None,
) -> SessionPayload:
    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No LTI session")
    session = await get_session(redis, cookie)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return session


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> HTMLResponse:
    """Course-wide accessibility dashboard for instructors."""

    session = await _require_session(redis, reflow_lti_session)
    target_course = course_id or session.course_id
    template = _load_template("dashboard.html")
    return HTMLResponse(template.replace("{{ course_id }}", target_course))


@router.get("", response_class=HTMLResponse)
async def review_index(
    request: Request,
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> HTMLResponse:
    """Render the pending-review list for the current course."""

    session = await _require_session(redis, reflow_lti_session)
    target_course = course_id or session.course_id
    template = _load_template("index.html")
    body = template.replace("{{ course_id }}", target_course)
    body = body.replace("{{ user_name }}", session.user_name or "Instructor")
    return HTMLResponse(body)


@router.get("/api/pending")
async def api_pending(
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """JSON list of jobs awaiting review for the current course."""

    session = await _require_session(redis, reflow_lti_session)
    target_course = course_id or session.course_id
    jobs = await list_pending(redis, target_course)
    rows = [
        {
            "reflow_job_id": j.reflow_job_id,
            "filename": j.canvas_file_name,
            "created_at": j.created_at,
            "canvas_page_url": j.canvas_page_url,
            "canvas_page_id": j.canvas_page_id,
        }
        for j in jobs
    ]
    return JSONResponse({"course_id": target_course, "jobs": rows})


@router.get("/{job_id}", response_class=HTMLResponse)
async def review_one(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> HTMLResponse:
    """Side-by-side review screen for a single document."""

    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")
    template = _load_template("one.html")
    body = (
        template.replace("{{ job_id }}", job.reflow_job_id)
        .replace("{{ filename }}", job.canvas_file_name)
        .replace("{{ canvas_page_url }}", job.canvas_page_url or "")
        .replace("{{ canvas_course_id }}", job.canvas_course_id)
    )
    return HTMLResponse(body)


@router.post("/{job_id}/approve")
async def approve(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")

    # When the API token lacks manage_wiki, the bridge worker skips Canvas
    # Page creation and leaves canvas_page_url empty. Approval is still
    # meaningful in that case - it transitions the job to "published" so
    # the panorama overlay serves the alt formats to students. We only
    # call publish_page when an actual Canvas Page exists.
    canvas = CanvasClient()
    if job.canvas_page_url:
        await canvas.publish_page(job.canvas_course_id, job.canvas_page_url)
    job.status = "published"
    await put_job(redis, job)
    return JSONResponse({"ok": True, "page_url": job.canvas_page_url or ""})


@router.post("/{job_id}/reject")
async def reject(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")

    if job.canvas_page_url:
        canvas = CanvasClient()
        try:
            await canvas.delete_page(job.canvas_course_id, job.canvas_page_url)
        except Exception:
            logger.exception("Failed to delete Canvas page during reject")
    job.status = "rejected"
    await put_job(redis, job)
    return JSONResponse({"ok": True})


def _load_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    if not path.exists():
        return f"<h1>Template missing: {name}</h1>"
    return path.read_text(encoding="utf-8")


def _build_data_payload(job_jobs: list[Any]) -> list[dict[str, Any]]:
    """Reserved helper for richer JSON; kept to keep the module focused."""

    return []
