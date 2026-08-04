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

from ..canvas.state import (
    get_course_optin,
    get_job,
    is_course_enabled,
    list_course_jobs,
    list_pending,
    put_job,
    set_course_enabled,
)
from ..config import settings
from ..dependencies import get_redis_client
from ..lti.routes import SESSION_COOKIE
from ..lti.session import SessionPayload, get_session
from ..utils.rate_limit import enforce_rate_limit

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
            # ``status`` lets the index.html row badge + the Review button
            # route to the right surface: ``awaiting_review`` jobs go to
            # the side-by-side accessibility review; ``awaiting_approval``
            # jobs go to the PII gate page.
            "status": j.status,
        }
        for j in jobs
    ]
    return JSONResponse({"course_id": target_course, "jobs": rows})


# Display buckets. Reflow's internal status vocabulary is finer-grained
# than anything a coordinator needs on a list screen: what they want to
# know is whether a document needs them, is still working, is finished, or
# has gone wrong. Keep the raw status in the payload for the detail views.
_BUCKET: dict[str, str] = {
    "awaiting_approval": "needs_you",
    "awaiting_review": "needs_you",
    "processing": "converting",
    "processing_queued": "converting",
    "queued": "converting",
    "pii_scanning": "converting",
    "published": "done",
    "page_failed": "attention",
    "failed": "attention",
    "rejected": "attention",
    "denied": "attention",
}

# Lower wins when one Canvas file has several job records — which happens
# whenever a document is resubmitted. Faculty intent and finished work beat
# stale failures, so a dead job never masks a live one.
_RANK: dict[str, int] = {
    "published": 0,
    "awaiting_review": 1,
    "awaiting_approval": 1,
    "processing": 2,
    "pii_scanning": 2,
    "processing_queued": 2,
    "queued": 2,
    "page_failed": 5,
    "rejected": 8,
    "failed": 9,
    "denied": 9,
}

# What the reader should meet first: their own outstanding decisions, then
# anything that went wrong, then work in flight, then finished documents.
_DISPLAY_ORDER: dict[str, int] = {
    "needs_you": 0,
    "attention": 1,
    "converting": 2,
    "done": 3,
}

# Plain-language reasons. The raw ``error`` string is written for
# operators and mentions job ids and worker names; none of that helps the
# person deciding what to do about a document.
_REASON: dict[str, str] = {
    "page_failed": (
        "Converted successfully, but Reflow could not write the Canvas page. "
        "The page may be locked or in a locked module."
    ),
    "rejected": "You declined this document. It was not published.",
    "denied": "The privacy review was denied, so no accessible version was made.",
}


@router.get("/api/files")
async def api_files(
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """Every document Reflow knows about in this course, with its state.

    ``/api/pending`` answers "what needs a decision"; this answers "where
    is everything", which is the question the review screen is actually
    opened to settle. One row per Canvas file, best job wins.
    """

    session = await _require_session(redis, reflow_lti_session)
    target_course = course_id or session.course_id

    best: dict[str, Any] = {}
    for job in await list_course_jobs(redis, target_course):
        fid = str(job.canvas_file_id or "")
        if not fid:
            continue
        current = best.get(fid)
        rank = (_RANK.get(str(job.status), 7), -float(job.created_at or 0))
        if current is None or rank < current[0]:
            best[fid] = (rank, job)

    rows = []
    for fid, (_, job) in best.items():
        raw_status = str(job.status)
        bucket = _BUCKET.get(raw_status, "converting")
        reason = _REASON.get(raw_status)
        if bucket == "attention" and reason is None:
            reason = (
                job.error
                or "Conversion did not finish. Re-upload the file to try again."
            )
        rows.append(
            {
                "canvas_file_id": fid,
                "reflow_job_id": job.reflow_job_id,
                "filename": job.canvas_file_name,
                "status": raw_status,
                "bucket": bucket,
                "reason": reason,
                "created_at": job.created_at,
                "canvas_page_url": job.canvas_page_url,
                # Only rows in ``needs_you`` carry an action URL; the
                # front end uses its absence to render plain text instead
                # of a button, so nothing looks clickable that isn't.
                "action_url": (
                    f"/canvas/review/{job.reflow_job_id}/pii"
                    if raw_status == "awaiting_approval"
                    else f"/canvas/review/{job.reflow_job_id}"
                    if raw_status == "awaiting_review"
                    else None
                ),
            }
        )

    # Display order is not the dedupe ranking. ``_RANK`` decides which job
    # best represents a file; this decides what the reader sees first, and
    # that is whatever is waiting on them, then whatever is broken.
    rows.sort(key=lambda r: (_DISPLAY_ORDER.get(r["bucket"], 9), r["filename"].lower()))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    return JSONResponse(
        {"course_id": target_course, "files": rows, "counts": counts}
    )


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


@router.get("/{job_id}/pii", response_class=HTMLResponse)
async def review_pii(
    job_id: str,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> HTMLResponse:
    """PII approval surface inside the LTI tool.

    Renders the same approval form the alt-route's panorama gate uses,
    but wrapped in a route the Accessible Documents queue can deep-link
    to. The form's POST target is the existing
    ``/canvas/panorama/pii-decision/{job_id}`` endpoint (CSRF-protected,
    session-scoped); the token is generated server-side here and
    embedded into the page so the submit actually succeeds.
    """
    session = await _require_session(redis, reflow_lti_session)
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != session.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")

    # Pull the latest findings from Reflow. If Reflow has already moved
    # the job past awaiting_approval (e.g., a parallel tab approved it
    # already, or the gate timed out and Reflow auto-decided), show a
    # short note so faculty isn't stuck on a dead form.
    from ..canvas.reflow_client import ReflowClient

    reflow = ReflowClient()
    try:
        status = await reflow.get_status(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PII page: reflow status fetch failed for %s: %s", job_id, exc)
        raise HTTPException(
            status_code=502, detail="Could not reach Reflow"
        ) from exc

    if status.get("status") != "awaiting_approval":
        return HTMLResponse(
            f"<main style='font-family:system-ui;padding:2rem;max-width:48rem;'>"
            f"<h1>This document is no longer awaiting PII review.</h1>"
            f"<p>Current Reflow status: <code>{status.get('status')}</code>. "
            f"You can return to the queue to see the next item.</p>"
            f"<p><a href='/canvas/review?course_id="
            f"{job.canvas_course_id}'>← Back to Accessible Documents</a></p>"
            f"</main>"
        )

    findings = status.get("pii_findings") or status.get("pii") or []
    from ._pii_approval_page import render_pii_approval_page
    from .canvas_panorama import _csrf_token_for

    page = render_pii_approval_page(
        job_id=job_id,
        file_name=job.canvas_file_name,
        course_id=job.canvas_course_id,
        findings=findings,
        decision_url=f"/canvas/panorama/pii-decision/{job_id}",
        csrf_token=_csrf_token_for(reflow_lti_session or ""),
    )
    return HTMLResponse(page)


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
    await enforce_rate_limit(redis, bucket="review_approve", actor=session.user_id, limit=30, window_seconds=60)

    # When the API token lacks manage_wiki, the bridge worker skips Canvas
    # Page creation and leaves canvas_page_url empty. Approval is still
    # meaningful in that case — it transitions the job to "published" so
    # the panorama overlay serves the alt formats to students. We only
    # call publish_page when an actual Canvas Page exists.
    publish_warning: str | None = None
    if job.canvas_page_url:
        from ..workers.reflow_bridge_worker import _canvas_client_for_job

        canvas = await _canvas_client_for_job(redis, job)
        try:
            await canvas.publish_page(job.canvas_course_id, job.canvas_page_url)
        except Exception as exc:  # noqa: BLE001
            # Surface the failure but still flip the status — the
            # connector-hosted alt formats become available immediately,
            # and the bridge keeps retrying the Canvas Page publish in
            # the background once the OAuth scope shows up.
            logger.warning(
                "publish_page failed during approve for job %s: %s",
                job_id, exc,
            )
            publish_warning = (
                "The accessible alt formats are now available to students "
                "through the LTI tool, but Canvas refused the Page publish. "
                "Re-run Authorize Reflow (Settings → Apps) so the OAuth "
                "token has page-write scope, then the bridge will publish "
                "the Page automatically on its next tick."
            )
    job.status = "published"
    await put_job(redis, job)
    return JSONResponse({
        "ok": True,
        "page_url": job.canvas_page_url or "",
        "warning": publish_warning,
    })


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
    await enforce_rate_limit(redis, bucket="review_reject", actor=session.user_id, limit=30, window_seconds=60)

    if job.canvas_page_url:
        from ..workers.reflow_bridge_worker import _canvas_client_for_job

        canvas = await _canvas_client_for_job(redis, job)
        try:
            await canvas.delete_page(job.canvas_course_id, job.canvas_page_url)
        except Exception:
            logger.exception("Failed to delete Canvas page during reject")
    job.status = "rejected"
    await put_job(redis, job)
    return JSONResponse({"ok": True})


_INSTRUCTOR_ROLE_HINTS = ("instructor", "teacher", "contentdeveloper", "administrator", "ta")


def _is_instructor(session: SessionPayload) -> bool:
    """True when the LTI roles claim indicates course-management rights.

    Roles arrive as full IMS URIs, e.g.
    ``http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor``. We
    substring-match the leaf rather than parsing, because Canvas emits
    several vocab namespaces and the leaf is stable across all of them.
    """
    joined = " ".join(session.roles or []).lower()
    return any(hint in joined for hint in _INSTRUCTOR_ROLE_HINTS)


@router.get("/api/optin")
async def api_optin_status(
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """Whether full-course scanning is on, and who turned it on."""
    session = await _require_session(redis, reflow_lti_session)
    target_course = course_id or session.course_id
    enabled = await is_course_enabled(redis, target_course)
    record = await get_course_optin(redis, target_course)
    return JSONResponse(
        {
            "course_id": target_course,
            "enabled": enabled,
            "required": bool(getattr(settings, "canvas_require_course_optin", True)),
            "drop_folder": str(getattr(settings, "canvas_drop_folder_name", "") or ""),
            "enabled_by": (record or {}).get("actor") or "",
            "enabled_at": (record or {}).get("at") or 0,
            "can_change": _is_instructor(session),
        }
    )


@router.post("/api/optin")
async def api_optin_set(
    payload: dict[str, Any],
    course_id: str | None = None,
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """Turn full-course scanning on or off for this course.

    Restricted to instructors: a student launching the tool must not be able
    to authorise processing of a whole course. The actor is recorded so
    "who authorised this" has an answer that isn't "the software".
    """
    session = await _require_session(redis, reflow_lti_session)
    if not _is_instructor(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only course instructors can change this setting.",
        )
    target_course = course_id or session.course_id
    if not target_course:
        raise HTTPException(status_code=400, detail="No course in session")

    await enforce_rate_limit(
        redis, bucket="course_optin", actor=session.user_id, limit=20, window_seconds=60,
    )

    enabled = bool(payload.get("enabled"))
    actor = session.user_email or session.user_id or ""
    await set_course_enabled(redis, target_course, enabled=enabled, actor=actor)
    logger.info(
        "course %s scanning %s by %s",
        target_course, "ENABLED" if enabled else "DISABLED", actor,
    )
    return JSONResponse({"ok": True, "course_id": target_course, "enabled": enabled})


def _load_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    if not path.exists():
        return f"<h1>Template missing: {name}</h1>"
    return path.read_text(encoding="utf-8")


def _build_data_payload(job_jobs: list[Any]) -> list[dict[str, Any]]:
    """Reserved helper for richer JSON; kept to keep the module focused."""

    return []
