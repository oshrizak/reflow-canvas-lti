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
from fastapi.responses import HTMLResponse, JSONResponse, Response
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


@router.get("/{job_id}/pdf")
async def review_pdf_proxy(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Response:
    """Same-origin proxy for the original Canvas PDF.

    Canvas Cloud's ``frame-ancestors`` CSP refuses to let external origins
    iframe its file viewer, so the review screen pulls the bytes through
    the connector instead. Auth: the instructor's LTI session cookie
    plus the job-belongs-to-this-course check; the fetch itself uses the
    job's stored OAuth token via the same client the bridge worker uses.
    """
    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")

    from ..workers.reflow_bridge_worker import _canvas_client_for_job

    client = await _canvas_client_for_job(redis, job)
    try:
        pdf_bytes = await client.download_file(job.canvas_file_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PDF proxy failed for job %s (file %s): %s",
            job_id, job.canvas_file_id, exc,
        )
        raise HTTPException(
            status_code=502, detail="Could not fetch source PDF from Canvas"
        ) from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            # ``inline`` keeps the browser's PDF viewer in-iframe; without
            # it, some browsers default to download for proxied PDFs.
            "Content-Disposition": f'inline; filename="{job.canvas_file_name}"',
        },
    )


@router.get("/{job_id}/canvas-page", response_class=HTMLResponse)
async def review_canvas_page_proxy(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> HTMLResponse:
    """Same-origin render of the live Canvas Page body.

    Once the bridge has successfully published, this surface shows the
    page as Canvas stores it (post-publish, post-edits-in-Canvas), not
    the connector's pre-publish HTML. The body is wrapped in a minimal
    HTML shell so it renders standalone — Canvas's own page chrome
    (nav, sidebars) is intentionally dropped. Inline images embedded by
    Canvas load cross-origin without issue; only iframing the page
    itself is blocked by Canvas's CSP.
    """
    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")
    if not job.canvas_page_url:
        return HTMLResponse(
            "<p style='font-family:system-ui;padding:1rem;color:#555;'>"
            "This Canvas Page hasn't been published yet. The accessible "
            "preview on the right is what will be created when you approve."
            "</p>"
        )

    from ..workers.reflow_bridge_worker import _canvas_client_for_job

    client = await _canvas_client_for_job(redis, job)
    try:
        page = await client.get_page(job.canvas_course_id, job.canvas_page_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Canvas Page proxy failed for job %s (page %s): %s",
            job_id, job.canvas_page_url, exc,
        )
        raise HTTPException(
            status_code=502, detail="Could not fetch Canvas Page"
        ) from exc

    body = page.get("body") or "<p>(Canvas Page has no body.)</p>"
    title = page.get("title") or job.canvas_file_name
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 1rem 1.5rem; line-height: 1.5; color: #222; }}
  img {{ max-width: 100%; height: auto; }}
  table {{ border-collapse: collapse; margin: 0.5rem 0; }}
  th, td {{ border: 1px solid #999; padding: 0.25rem 0.5rem; }}
  h1, h2, h3 {{ line-height: 1.25; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
    )


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
