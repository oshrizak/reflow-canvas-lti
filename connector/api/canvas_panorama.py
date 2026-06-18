"""HTTP endpoints for the Panorama-style overlay."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from redis.asyncio import Redis

from ..canvas.alt_formats import (
    canonical_html,
    html_full_document,
    html_to_plain_text,
    render_audio_mp3,
    render_braille_brf,
    render_epub,
    render_ocr_pdf,
    render_reader_html,
    render_translation,
)
from ..canvas.client import CanvasClient
from ..canvas.markdown_to_html import RenderedPage
from ..canvas.panorama import (
    Issue,
    Score,
    issues_from_reflow_result,
    source_accessibility_estimate,
)
from ..canvas.reflow_client import ReflowClient
from ..canvas.sanitize import sanitize_html
from ..canvas.state import (
    append_approval_event,
    clear_edited_html,
    clear_processed,
    get_edited_html,
    get_job,
    list_approval_events,
    put_edited_html,
    put_job,
)
from ..canvas.tenant import tk
from ..canvas.wcag_checks import run_wcag_checks
from ..config import settings
from ..dependencies import get_redis_client
from ..lti.routes import SESSION_COOKIE
from ..lti.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/canvas/panorama", tags=["canvas-panorama"])

SCORE_CACHE_KEY = tk("canvas:score:{job_id}")
SCORE_CACHE_TTL = 24 * 3600

# Every format we can currently deliver. Source of truth shared with the
# bundle's modal. Translation is parameterised (e.g. ``translate:es``).
LIVE_FORMATS = ["html", "html-math", "txt", "markdown", "epub", "audio", "translate", "ocr", "immersive", "braille"]


@router.get("/handshake")
async def handshake(request: Request, inst: str = Query(...)) -> JSONResponse:
    return JSONResponse(
        {
            "inst": inst,
            "expires_at": int(time.time()) + 3600,
            "endpoints": {
                "score": "/canvas/panorama/score",
                "issues": "/canvas/panorama/issues",
                "alt": "/canvas/panorama/alt",
                "edit": "/canvas/panorama/edit",
            },
        }
    )


@router.get("/score")
async def score(
    course_id: str = Query(...),
    file_ids: str = Query(...),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    ids = [f.strip() for f in file_ids.split(",") if f.strip()]
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="Too many file_ids; cap is 50")
    out: dict[str, dict[str, Any]] = {}
    for fid in ids:
        job_id = await _lookup_job_for_file(redis, course_id, fid)
        if job_id is None:
            out[fid] = {"status": "unscanned"}
            continue
        out[fid] = await _build_score_payload(redis, job_id)
    return JSONResponse({"course_id": course_id, "scores": out})


@router.get("/scored_files")
async def scored_files(
    course_id: str = Query(...),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """All scored files in a course keyed by display filename.

    When the same filename appears in multiple canvas-bridge records
    (e.g. faculty re-uploaded the PDF), we surface the **best**
    candidate per filename using the same priority rank as
    ``_lookup_job_for_file``. This prevents a stale failed job from
    masking a successful re-conversion of the same document.
    """
    jobs = await _all_canvas_jobs_for_course(redis, course_id)
    # Group by filename, then pick the best candidate per group.
    by_name: dict[str, list[dict[str, Any]]] = {}
    for data in jobs:
        filename = str(data.get("canvas_file_name") or "")
        if not filename:
            continue
        by_name.setdefault(filename, []).append(data)

    out: dict[str, dict[str, Any]] = {}
    for filename, candidates in by_name.items():
        candidates.sort(key=_job_rank)
        best = candidates[0]
        job_id = str(best.get("reflow_job_id"))
        payload = await _build_score_payload(redis, job_id)
        payload["job_id"] = job_id
        payload["canvas_file_id"] = best.get("canvas_file_id")
        payload["edited"] = bool(await get_edited_html(redis, job_id))
        out[filename] = payload
    return JSONResponse({"course_id": course_id, "by_filename": out})


@router.get("/score_by_job")
async def score_by_job(
    job_ids: str = Query(...),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Score payloads keyed by reflow job id.

    The overlay uses this to decorate the *accessible-version* link in a
    published Canvas Page (``/canvas/panorama/alt/{job_id}/html``). Unlike
    ``/score`` — which resolves a Canvas *file* id to a job and shows the
    original PDF's source estimate — this returns the job directly so the
    accessible link can show the WCAG accessibility of the generated output
    (the "after" number).
    """
    ids = [j.strip() for j in job_ids.split(",") if j.strip()]
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="Too many job_ids; cap is 50")
    out: dict[str, dict[str, Any]] = {}
    for jid in ids:
        out[jid] = await _build_score_payload(redis, jid)
    return JSONResponse({"scores": out})


@router.get("/oauth_status")
async def oauth_status(
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Whether the current instructor has authorized Reflow against Canvas.

    The overlay calls this on load. Authorization is per (platform, user):
    without it the watcher can't read this instructor's course files and the
    bridge can't publish accessible Pages on their behalf. When
    ``is_instructor`` is true and ``authorized`` is false, the overlay shows
    an "Authorize Reflow" prompt (opened in a popup, not a full-window nav).

    Always 200 with a JSON body — the overlay treats a missing/expired
    session as simply "not authorized" rather than an error.
    """
    from ..canvas.user_oauth import get_user_token
    from ..lti.platform_store import get_platform_for_course

    out: dict[str, Any] = {
        "has_session": False,
        "is_instructor": False,
        "authorized": False,
    }
    if not session_id:
        return JSONResponse(out)
    sess = await get_session(redis, session_id)
    if sess is None:
        return JSONResponse(out)
    out["has_session"] = True
    out["is_instructor"] = any(
        ("Instructor" in r) or ("Teacher" in r) or ("TeachingAssistant" in r)
        for r in (sess.roles or [])
    )
    if not sess.course_id:
        return JSONResponse(out)
    platform_id = await get_platform_for_course(redis, sess.course_id)
    if not platform_id:
        return JSONResponse(out)
    try:
        token = await get_user_token(redis, platform_id, sess.user_id)
    except Exception:  # noqa: BLE001
        token = None
    # Authorized if a token exists and is either still valid or renewable
    # via a stored refresh_token (silent refresh on next use).
    if token is not None and (
        not token.is_expired() or bool(getattr(token, "refresh_token", ""))
    ):
        out["authorized"] = True
    return JSONResponse(out)


@router.get("/issues/{file_id}")
async def issues(
    file_id: str,
    course_id: str = Query(...),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    job_id = await _lookup_job_for_file(redis, course_id, file_id)
    if job_id is None:
        return JSONResponse({"file_id": file_id, "issues": []})
    reflow = ReflowClient()
    status = await reflow.get_status(job_id)
    signals = status.get("signals") or {}
    items: list[Issue] = issues_from_reflow_result(signals) if signals else []
    return JSONResponse({"file_id": file_id, "issues": [asdict(i) for i in items]})


@router.get("/wcag/{job_id}")
async def wcag_report(
    job_id: str,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Phase 7a: automated WCAG checks against the job's rendered HTML.

    Returns a structured report with per-rule findings and an overall
    ``passed`` boolean. Used by the panorama overlay to populate the
    reviewer's pre-publish checklist and by ``approve_job`` to enforce
    the publication gate. Requires instructor auth -- the report
    surfaces the rendered HTML's structure which we don't want public.
    """
    _job, _sess = await _require_instructor(redis, session_id, job_id)
    rendered = await _resolve_html(redis, job_id)
    full = html_full_document(rendered, mathjax=False)
    report = run_wcag_checks(full)
    return JSONResponse(report.to_json())


# ---- Editable accessible HTML --------------------------------------------

@router.get("/edit/{job_id}")
async def get_edit(
    job_id: str,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Return the current editable HTML for a job.

    Requires an LTI session bound to the same Canvas course as the job
    AND an instructor-class role (Teacher/TA/Admin). Returns the
    faculty-edited HTML when one exists, otherwise the auto-generated
    canonical HTML rendered from Reflow's markdown.
    """
    # Auth gate -- same check approve/reject use. Raises 401/403/404.
    job, _sess = await _require_instructor(redis, session_id, job_id)

    edited = await get_edited_html(redis, job_id)
    if edited:
        return JSONResponse({"job_id": job_id, "edited": True, "html": edited,
                             "title": job.canvas_file_name})

    # Fall back to canonical HTML built from Reflow markdown.
    rendered = await _build_canonical_html(redis, job_id)
    return JSONResponse({"job_id": job_id, "edited": False, "html": rendered.html,
                         "title": rendered.title})


@router.put("/edit/{job_id}")
async def put_edit(
    job_id: str,
    request: Request,
    body: dict = Body(...),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Save a faculty-edited HTML body. Requires instructor + CSRF token.

    Phase 5 sanitization is applied: incoming HTML runs through the
    academic-content allowlist (see ``src/canvas/sanitize.py``) before
    storage. ``<script>``, ``on*=`` handlers, ``javascript:`` URLs,
    and unknown tags are stripped. If sanitization leaves an empty
    body, we return 400 rather than store a useless edit.
    """
    _job, _sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    html = (body or {}).get("html")
    if not isinstance(html, str) or not html.strip():
        raise HTTPException(status_code=400, detail="Body must include non-empty 'html'")
    if len(html) > 5_000_000:
        raise HTTPException(status_code=413, detail="Edited HTML too large (5MB cap)")
    # Phase 5: sanitize against the academic-content allowlist before
    # persisting. Any <script>, on*=, javascript:, or unknown tag is
    # stripped here so downstream reads (panorama overlay, students)
    # never see active content from a possibly-compromised editor.
    try:
        clean = sanitize_html(html)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not clean.strip():
        raise HTTPException(
            status_code=400,
            detail="Edited HTML was empty after sanitization (only disallowed content was present)",
        )
    await put_edited_html(redis, job_id, clean)
    return JSONResponse({"job_id": job_id, "edited": True})


@router.delete("/edit/{job_id}")
async def delete_edit(
    job_id: str,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Discard the faculty edit; subsequent fetches return canonical HTML.

    Same auth + CSRF gates as PUT.
    """
    _job, _sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    await clear_edited_html(redis, job_id)
    return JSONResponse({"job_id": job_id, "edited": False})


# ---- Alternative formats -------------------------------------------------

@router.get("/alt/{job_id}/{fmt}")
async def alt_format(
    job_id: str,
    fmt: str,
    preview: bool = Query(default=False),
    redis: Redis = Depends(get_redis_client),
) -> Response:
    """Serve any supported alternative format derived from the canonical HTML.

    Faculty-approval gate: only ``status == "published"`` jobs are visible to
    students. Instructors can preview drafts by passing ``?preview=1`` from the
    Alt Formats modal. The front-end sets this automatically when the current
    user has an Instructor role.
    """
    job = await get_job(redis, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")

    # Faculty approval gate — students never see drafts.
    # Status values:
    #   "processing"       — pipeline running, no output yet
    #   "awaiting_review"  — faculty needs to approve before students can see
    #   "published"        — approved, all alt formats live
    #   "rejected" | "failed" — terminal, never expose to students
    if job.status != "published" and not preview:
        msg = {
            "processing": "Reflow is still processing this document. Please check back shortly.",
            "awaiting_review": "This accessible version is pending faculty review.",
            "rejected": "The faculty member chose not to publish this alternative version.",
            "failed": "Reflow could not generate an accessible version of this document.",
        }.get(job.status, "This alternative version is not available.")
        raise HTTPException(status_code=403, detail=msg)

    # PII gate: before attempting to render anything, check whether the
    # upstream Reflow job is paused at awaiting_approval. If it is, the
    # faculty member needs to make a privacy call before we can proceed.
    # For interactive HTML formats we render the approval UI inline; for
    # downloadable formats (epub, audio, braille) we return a clear 409
    # pointing the caller back at the HTML URL so they can act on it.
    pii_gate = await _maybe_pii_gate_response(job_id, job, fmt)
    if pii_gate is not None:
        return pii_gate

    # Markdown bypasses HTML — it's a separate pivot for power users.
    if fmt == "markdown":
        markdown = await _fetch_markdown(redis, job_id)
        return PlainTextResponse(markdown, media_type="text/markdown")

    # All other formats derive from canonical or edited HTML.
    rendered = await _resolve_html(redis, job_id)

    if fmt == "html":
        return HTMLResponse(html_full_document(rendered, mathjax=False))
    if fmt == "html-math":
        return HTMLResponse(html_full_document(rendered, mathjax=True))
    if fmt == "txt":
        return PlainTextResponse(html_to_plain_text(rendered))
    if fmt == "epub":
        try:
            data = render_epub(rendered)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            data,
            media_type="application/epub+zip",
            headers={"Content-Disposition": f"attachment; filename=\"{_safe(job.canvas_file_name)}.epub\""},
        )
    if fmt == "audio":
        try:
            audio = render_audio_mp3(rendered)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(audio, media_type="audio/mpeg",
                        headers={"Content-Disposition": f"attachment; filename=\"{_safe(job.canvas_file_name)}.mp3\""})
    if fmt == "immersive":
        # Immersive-Reader-style standalone HTML using browser Web Speech API.
        return HTMLResponse(render_reader_html(rendered))

    if fmt == "braille":
        try:
            brf = render_braille_brf(rendered)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            brf,
            media_type="application/x-brf",
            headers={"Content-Disposition": f"attachment; filename=\"{_safe(job.canvas_file_name)}.brf\""},
        )

    if fmt == "ocr" or fmt == "pdf-tagged":
        # Searchable (OCR) PDF. Operates on the original PDF bytes, fetched
        # from Canvas via the source file id stored on the job. ``pdf-tagged``
        # is accepted as a legacy alias for the same output (older page stubs
        # / cached links may still request it).
        if not job.canvas_file_id:
            raise HTTPException(status_code=409, detail="No source file id on job")
        canvas = CanvasClient()
        try:
            pdf_bytes = await canvas.download_file(str(job.canvas_file_id))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not fetch source PDF: {exc}") from exc
        try:
            ocr_pdf = render_ocr_pdf(pdf_bytes, archival=True)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            ocr_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=\"{_safe(job.canvas_file_name)}.searchable.pdf\""},
        )

    if fmt.startswith("translate:"):
        lang = fmt.split(":", 1)[1] or "es"
        try:
            translated = await render_translation(rendered, lang)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return HTMLResponse(html_full_document(translated))

    raise HTTPException(status_code=404, detail=f"Format not available: {fmt}")


# ---- Helpers -------------------------------------------------------------

async def _build_score_payload(redis: Redis, job_id: str) -> dict[str, Any]:
    """Compute or read cached score + available formats for a job."""
    cached = await redis.get(SCORE_CACHE_KEY.format(job_id=job_id))
    if cached:
        payload = json.loads(cached)
    else:
        try:
            s = await _compute_score(job_id, redis=redis)
            payload = asdict(s)
            await redis.set(
                SCORE_CACHE_KEY.format(job_id=job_id),
                json.dumps(payload),
                ex=SCORE_CACHE_TTL,
            )
        except Exception:
            logger.exception("Score computation failed for %s", job_id)
            # Even on score-computation failure, surface enough metadata for
            # the front-end to render the approval bar / DRAFT badge. The
            # approval flow only depends on job.status, not on the score.
            fallback_job = await get_job(redis, job_id)
            fb = {
                "status": "processing",
                "job_id": job_id,
                "available_formats": list(LIVE_FORMATS),
                "job_status": fallback_job.status if fallback_job else "processing",
            }
            if fallback_job is not None and getattr(fallback_job, "error", None):
                fb["error"] = fallback_job.error
            return fb
    # Always overwrite available_formats from the live LIVE_FORMATS list so
    # new format renderers light up as soon as they're added server-side.
    payload["available_formats"] = list(LIVE_FORMATS)
    # Respect ``score_from_reflow_result``'s status ("scored" vs
    # "unscanned"); the previous unconditional override forced every
    # row to "scored" and contributed to the misleading flat-15 display.
    payload.setdefault("status", "scored")
    payload["job_id"] = job_id
    # Surface the job's lifecycle status separately ("awaiting_review",
    # "published", "rejected", "processing", "failed") so the front-end
    # can render the approval bar / preview gate.
    job = await get_job(redis, job_id)
    if job is not None:
        payload["job_status"] = job.status
        # Before→after story. ``score`` (above) is the WCAG accessibility of
        # the generated accessible version — the "after". Here we attach a
        # coarse estimate of the ORIGINAL PDF's accessibility — the "before"
        # — derived from how the pipeline classified the source (scanned vs
        # born-digital, OCR'd, text layer present). The overlay shows the
        # source estimate on the Original-PDF dial and the WCAG score on the
        # accessible version, so faculty see the lift the conversion gave.
        src_score, src_sev = source_accessibility_estimate(getattr(job, "signals", None))
        if src_score is not None:
            payload["source_score"] = src_score
            payload["source_severity"] = src_sev
        # Surface the failure reason so the overlay can *tell the
        # professor why* a file isn't ready, instead of rendering a
        # blank dial. Only set when present; students never see this
        # (the front-end gates the status dial to instructors).
        if getattr(job, "error", None):
            payload["error"] = job.error
        # Phase 8: surface the Canvas Page URL so the panorama overlay
        # can promote the published Canvas Page as the *primary*
        # student-facing artifact. The PDF link in Canvas's Files UI
        # is still shown but labeled "Original PDF (source copy)".
        if getattr(job, "canvas_page_url", None):
            payload["canvas_page_url"] = job.canvas_page_url
        if getattr(job, "canvas_page_id", None):
            payload["canvas_page_id"] = job.canvas_page_id
    return payload


# Lower number = more "deliverable" job state. We pick the lowest score
# among candidate jobs, and among ties take the most recently created.
# Faculty intent (published / awaiting_review / processing) always beats
# terminal failures, so a stale failed job for an old file_id never wins
# over a fresh-but-still-converting job for the same document.
_STATUS_PRIORITY: dict[str, int] = {
    "published": 0,
    "awaiting_review": 1,
    "processing": 2,
    "awaiting_approval": 2,
    "pii_scanning": 2,
    "processing_queued": 2,
    # Conversion succeeded but the Canvas Page write was rejected (missing
    # Pages scope). Ranks below in-flight states (a still-converting job may
    # yet produce a page) but above rejected/failed so it surfaces over a
    # stale failed sibling and faculty see the actionable reason.
    "page_failed": 5,
    "rejected": 8,  # rejected reflects faculty intent; rank above failed
    "failed": 9,
    "denied": 9,
}


def _job_rank(data: dict[str, Any]) -> tuple[int, float]:
    """Sort key: lower priority + newer first.

    Returns ``(priority, -created_at)`` so Python's ascending sort puts
    the best candidate at index 0. Unknown statuses fall to the bottom.
    """
    status = str(data.get("status") or "").lower()
    pri = _STATUS_PRIORITY.get(status, 7)
    try:
        ts = float(data.get("created_at") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    # Negate ts so newer (larger) is preferred among equal-priority rows.
    return (pri, -ts)


async def _all_canvas_jobs_for_course(
    redis: Redis, course_id: str
) -> list[dict[str, Any]]:
    """Walk every canvas-bridge record for one course. Cached per request."""
    out: list[dict[str, Any]] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(
            cursor=cursor, match=tk("canvas:job:*"), count=200
        )
        for raw in keys:
            key = raw.decode() if isinstance(raw, bytes) else raw
            raw_val: Any = await redis.get(key)
            if not raw_val:
                continue
            try:
                data = json.loads(raw_val)
            except (ValueError, TypeError):
                continue
            if str(data.get("canvas_course_id")) != str(course_id):
                continue
            out.append(data)
        if cursor == 0:
            return out


async def _lookup_job_for_file(
    redis: Redis, course_id: str, canvas_file_id: str
) -> str | None:
    """Best-job resolver for ``(course_id, canvas_file_id)``.

    Why this isn't just "first match on file_id":
    when a faculty member re-uploads the same PDF, Canvas issues a new
    ``canvas_file_id`` each time but the ``canvas_file_name`` stays
    constant. The Files UI may surface only one of those entries, and
    not necessarily the one whose conversion succeeded. To do the
    intuitive thing, we widen the search to include all canvas jobs in
    the course sharing this file's filename, then pick the best.

    Algorithm:
      1. Collect every canvas-bridge record in this course.
      2. If any matches by ``canvas_file_id``, use its ``canvas_file_name``
         to widen the candidate set to all jobs with the same filename.
         This handles the re-upload case where the anchor record happens
         to be a failed early attempt.
      3. Rank candidates by status priority (``published`` >
         ``awaiting_review`` > ``processing`` > terminal failures), then
         by recency. Return the winner's reflow_job_id.
      4. If no file_id match exists at all, return None — the watcher
         hasn't seen this file yet.
    """
    jobs = await _all_canvas_jobs_for_course(redis, course_id)
    if not jobs:
        return None
    fid = str(canvas_file_id)
    # 1. Direct file_id matches
    fid_matches = [j for j in jobs if str(j.get("canvas_file_id")) == fid]
    if fid_matches:
        # Use the filename from any anchor record to widen the search.
        filename = None
        for j in fid_matches:
            fn = j.get("canvas_file_name")
            if fn:
                filename = str(fn)
                break
        if filename:
            candidates = [
                j
                for j in jobs
                if str(j.get("canvas_file_name") or "") == filename
            ]
        else:
            candidates = fid_matches
    else:
        return None
    candidates.sort(key=_job_rank)
    winner = candidates[0]
    return str(winner.get("reflow_job_id"))


def _accessibility_score_from_wcag(report: Any) -> int:
    """0-100 accessibility score from a WCAG check report on the OUTPUT HTML.

    Errors weigh heavily (real blockers — missing alt text, no headings,
    missing language); warnings lightly. A clean document scores 100, floored
    at 0. This is what the dial shows: the accessibility of what students
    actually receive, so faculty edits (e.g. adding alt text) move the number.
    """
    errors = sum(1 for f in report.findings if f.severity == "error")
    warnings = sum(1 for f in report.findings if f.severity == "warning")
    return max(0, min(100, 100 - 12 * errors - 3 * warnings))


async def _compute_score(job_id: str, redis: Redis | None = None) -> Score:
    """Accessibility score for the dial: run the WCAG checks on the generated
    HTML (the accessible output students receive) and score it.

    This scores the OUTPUT's accessibility, not the source PDF's conversion
    fidelity — so a clean accessible page reads high, problems (missing alt
    text, headings, language) pull it down, and faculty edits move the number.
    Per-file variance is real because it reflects each document's actual HTML.
    Falls back to 'unscanned' when the HTML can't be resolved yet (job not
    completed, or Reflow unavailable) so we never fabricate a number.

    (The older conversion-quality heuristic — ``score_from_reflow_result`` over
    ``job.signals`` — is retained in the codebase but no longer drives the dial;
    it pinned most documents at ~80 because the image-alt signal failed broadly.)
    """
    from ..canvas.panorama import severity_of

    if redis is None:
        return Score(score=None, severity=None,
                     available_formats=list(LIVE_FORMATS), status="unscanned")
    try:
        rendered = await _resolve_html(redis, job_id)
        report = run_wcag_checks(html_full_document(rendered, mathjax=False))
        score = _accessibility_score_from_wcag(report)
        return Score(score=score, severity=severity_of(score),
                     available_formats=list(LIVE_FORMATS), status="scored")
    except Exception:
        logger.exception("Accessibility (WCAG) score failed for %s", job_id)
        return Score(score=None, severity=None,
                     available_formats=list(LIVE_FORMATS), status="unscanned")


async def _fetch_markdown(redis: Redis, job_id: str) -> str:
    reflow = ReflowClient()
    status = await reflow.get_status(job_id)
    if status.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Job not yet completed (status: {status.get('status')})",
        )
    markdown_url = status.get("markdown_url") or status.get("result_url")
    if not markdown_url:
        raise HTTPException(status_code=500, detail="Completed job is missing markdown_url")
    return await reflow.fetch_markdown(markdown_url)


async def _maybe_pii_gate_response(
    job_id: str,
    job: Any,
    fmt: str,
) -> Response | None:
    """If the Reflow job is paused at awaiting_approval, return a gate response.

    For ``fmt in {"html", "html-math"}`` we render the inline approval UI
    so faculty can act without leaving the browser. For every other format
    we 409 with a structured payload that includes the URL to the gate
    page; the front-end can deep-link there from a button in the alt-
    formats modal.

    Returns ``None`` when the job is not paused on PII review (the caller
    proceeds to normal rendering). Returns a Response otherwise.

    Failure modes are tolerant: if Reflow itself errors here, we let the
    downstream renderer (which calls Reflow again) surface the real
    error rather than masking it with a misleading "PII gate" message.
    """
    from ._pii_approval_page import render_pii_approval_page

    try:
        reflow = ReflowClient()
        status = await reflow.get_status(job_id)
    except Exception as exc:  # noqa: BLE001 — best-effort gate check
        logger.debug("PII gate check skipped (Reflow status unavailable): %s", exc)
        return None

    if status.get("status") != "awaiting_approval":
        return None

    findings = status.get("pii_findings") or status.get("pii") or []
    file_name = getattr(job, "canvas_file_name", None)
    course_id = getattr(job, "canvas_course_id", None)
    decision_url = f"/canvas/panorama/pii-decision/{job_id}"

    if fmt in ("html", "html-math"):
        page = render_pii_approval_page(
            job_id=job_id,
            file_name=file_name,
            course_id=course_id,
            findings=findings,
            decision_url=decision_url,
        )
        # 200 so the iframe doesn't render an error chrome around the page;
        # the body itself communicates the gated state.
        return HTMLResponse(page, status_code=200)

    # Non-interactive format. The 409 payload tells the front-end exactly
    # where to send the faculty member so they can complete the gate.
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Job is paused awaiting PII review. Open the HTML "
            "view to approve or deny processing.",
            "status": "awaiting_approval",
            "approval_url": (
                f"/canvas/panorama/alt/{job_id}/html"
            ),
            "pii_finding_count": len(findings),
        },
    )


async def _build_canonical_html(redis: Redis, job_id: str) -> RenderedPage:
    """Auto-generate the canonical accessible HTML from Reflow output.

    Phase 5 sanitization is applied to the rendered HTML body before
    returning. The AI conversion pipeline isn't supposed to emit
    ``<script>`` -- but "supposed to" isn't a security control, and the
    pipeline IS a remote service we don't fully trust. Same allowlist
    as the edit path; consistent behavior keeps the testable surface
    tractable.
    """
    job = await get_job(redis, job_id)
    title = (job.canvas_file_name if job else "Document").rsplit(".pdf", 1)[0]
    markdown = await _fetch_markdown(redis, job_id)
    # Embed Canvas-hosted figures. The bridge uploads each figure into a
    # course folder and records the markdown-ref -> Canvas-file-URL map on the
    # job; rewrite the relative ``figures/<id>.png`` refs to those Canvas URLs
    # so these accessible-HTML views use the same self-contained images as the
    # Canvas Page. (The old figure proxy is gone — Canvas serves the images.)
    fig_map = (getattr(job, "figure_canvas_urls", None) if job else None) or {}
    for ref, fig_url in fig_map.items():
        markdown = markdown.replace(ref, fig_url)
    rendered = canonical_html(markdown, title=title)
    # Wrap the rendered page through sanitize before passing it back.
    # RenderedPage is a simple dataclass; mutate ``html`` in place.
    try:
        rendered.html = sanitize_html(rendered.html)
    except RuntimeError:
        logger.exception("sanitize_html failed on canonical render for %s", job_id)
    return rendered


async def _resolve_html(redis: Redis, job_id: str) -> RenderedPage:
    """Prefer faculty-edited HTML; fall back to canonical."""
    edited = await get_edited_html(redis, job_id)
    job = await get_job(redis, job_id)
    title = (job.canvas_file_name if job else "Document").rsplit(".pdf", 1)[0]
    if edited:
        return RenderedPage(title=title, html=edited)
    return await _build_canonical_html(redis, job_id)


def _safe(name: str | None) -> str:
    """Sanitize for Content-Disposition filename headers."""
    if not name:
        return "document"
    return "".join(c if c.isalnum() or c in "-._ " else "_" for c in name).strip()


# ---------------------------------------------------------------------------
# Faculty approval flow — explicit approve / reject / request-edits gate
# ---------------------------------------------------------------------------
# Reflow always lands new conversions in awaiting_review state. Instructors
# review the auto-generated HTML in the in-modal editor; once they're happy,
# they hit Approve and the alt formats become visible to students. Every
# transition is audit-logged with actor + timestamp + optional comment so
# the ISO can pull a complete history at any time.


async def _require_instructor(
    redis: Redis,
    session_id: str | None,
    job_id: str,
):
    """Return (job, session) when the caller is an instructor in the right
    course; otherwise raise. Centralizes the auth check for approve/reject.

    On success, binds the session's ``user_id`` and ``course_id`` into the
    logging contextvars so every line emitted by the downstream handler
    carries the actor identity automatically.
    """
    from ..logging_setup import course_id_var, user_id_var

    if not session_id:
        raise HTTPException(status_code=401, detail="No LTI session")
    sess = await get_session(redis, session_id)
    if sess is None:
        raise HTTPException(status_code=401, detail="Expired LTI session")
    is_instructor = any(
        r.endswith("Instructor")
        or r.endswith("Administrator")
        or "TeachingAssistant" in r
        or "Admin" in r
        for r in (sess.roles or [])
    )
    if not is_instructor:
        raise HTTPException(status_code=403, detail="Instructor role required")
    job = await get_job(redis, job_id)
    if job is None or job.canvas_course_id != sess.course_id:
        raise HTTPException(status_code=404, detail="Unknown job")

    # Identity is now established; bind it for the remainder of the request.
    user_id_var.set(sess.user_id)
    course_id_var.set(sess.course_id)

    return job, sess


# --- CSRF + origin allowlist ---------------------------------------------
#
# CSRF token strategy: HMAC the session_id with a server-side secret.
# This is stateless (no separate Redis lookup) and naturally rotates
# when the LTI session rotates. The token is fetched once by the
# panorama JS via GET /canvas/panorama/csrf and echoed back on every
# state-changing request as the X-CSRF-Token header. Without a valid
# session cookie there's no token to derive, so the same code path
# fails closed.
#
# Origin allowlist: in the LTI iframe context the browser always sends
# an Origin header matching the Canvas host. We compare against
# ``settings.canvas_allowed_origins`` (comma-separated list). When
# unset we fall back to *permissive* in dev only; production deploys
# MUST set the env var or the gate will refuse cross-origin writes.

import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402


def _csrf_secret() -> bytes:
    """Return the secret used to HMAC CSRF tokens.

    We prefer an explicit ``csrf_secret_key`` setting; fall back to the
    LTI private key fingerprint so a freshly-bootstrapped dev env still
    has a stable derivation source without extra config.
    """
    raw = getattr(settings, "csrf_secret_key", "") or ""
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    if raw:
        return str(raw).encode("utf-8")
    # Fallback: hash the LTI public key path + a fixed app constant.
    # Stable across restarts in a given deploy. Not "secret" in a
    # cryptographic sense, but raises the bar over a hardcoded constant.
    seed = f"equalify-reflow:csrf:{getattr(settings, 'lti_public_key_path', '')}"
    return _hashlib.sha256(seed.encode("utf-8")).digest()


def _csrf_token_for(session_id: str) -> str:
    return _hmac.new(_csrf_secret(), session_id.encode("utf-8"), _hashlib.sha256).hexdigest()


def _require_csrf(session_id: str | None, supplied: str | None) -> None:
    if not session_id:
        raise HTTPException(status_code=401, detail="No LTI session")
    if not supplied:
        raise HTTPException(status_code=403, detail="Missing X-CSRF-Token header")
    expected = _csrf_token_for(session_id)
    # ``hmac.compare_digest`` for constant-time compare.
    if not _hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _allowed_origins() -> set[str]:
    raw = (getattr(settings, "canvas_allowed_origins", "") or "").strip()
    if not raw:
        return set()
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


def _require_trusted_origin(request: Request) -> None:
    """Reject cross-origin state-changing requests in production.

    Two configuration paths, both honoured:
      * ``CANVAS_ALLOWED_ORIGINS`` -- comma-separated literal list.
      * ``CANVAS_ALLOWED_ORIGIN_REGEX`` -- single regex; takes
        precedence over the literal list when both are set.

    Dev fallback: if neither is configured we let any Origin through.
    Production deployments MUST configure one or the other.
    """
    import re as _re
    origin = (request.headers.get("origin") or "").rstrip("/")
    referer = (request.headers.get("referer") or "").rstrip("/")

    regex_str = (getattr(settings, "canvas_allowed_origin_regex", "") or "").strip()
    if regex_str:
        try:
            pattern = _re.compile(regex_str)
        except _re.error as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Bad CANVAS_ALLOWED_ORIGIN_REGEX: {exc}",
            ) from exc
        if pattern.fullmatch(origin):
            return
        if referer:
            from urllib.parse import urlparse
            try:
                u = urlparse(referer)
                base = f"{u.scheme}://{u.netloc}"
                if pattern.fullmatch(base):
                    return
            except Exception:
                pass
        raise HTTPException(
            status_code=403,
            detail=f"Origin not allowed by regex (got {origin!r})",
        )

    allowed = _allowed_origins()
    if not allowed:
        return  # dev fallback; production MUST configure
    if origin in allowed:
        return
    if referer:
        from urllib.parse import urlparse
        try:
            u = urlparse(referer)
            base = f"{u.scheme}://{u.netloc}"
            if base in allowed:
                return
        except Exception:
            pass
    raise HTTPException(
        status_code=403,
        detail=f"Origin not allowed (got {origin!r})",
    )


@router.get("/csrf")
async def get_csrf(
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> JSONResponse:
    """Return a CSRF token bound to the current LTI session.

    The panorama bundle fetches this once at init time and echoes the
    token in the ``X-CSRF-Token`` header on every state-changing
    request. Returns 401 when no session cookie is present.
    """
    if not session_id:
        raise HTTPException(status_code=401, detail="No LTI session")
    return JSONResponse({"csrf_token": _csrf_token_for(session_id)})


async def _read_json_body(request: Request) -> dict:
    """Read the request body as JSON, returning {} on failure.

    The approve_job endpoint already takes ``comment`` via Body(...);
    we want to also pull ``waivers`` and ``checklist`` without
    breaking that signature. Re-reading the raw body works because
    FastAPI caches it on the Request after first access.
    """
    try:
        return await request.json()
    except Exception:
        return {}


def _client_ip(request: Request) -> str | None:
    """Return the client IP for audit logging.

    Honours ``X-Forwarded-For`` only when explicit - we expect to run
    behind a TLS-terminating ALB or ngrok, both of which set it. Falls
    back to the socket peer, which is correct for direct connections.
    Anything that looks bogus (commas-only, empty) returns None so the
    audit row records "unknown" instead of garbage.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return request.client.host
    return None


@router.post("/pii-decision/{job_id}")
async def pii_decision(
    job_id: str,
    request: Request,
    decision: str = Body(..., embed=True),
    justification: str = Body(..., embed=True),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Faculty decision on a PII-paused Reflow job.

    Auth: requires an instructor session bound to the job's course
    (same check as approve/reject). The instructor's identity is what
    gets stamped on the audit trail — Reflow's underlying token-based
    approval endpoint is bypassed because we already authenticated the
    actor via the LTI session and a token doesn't fit the LMS flow.

    Decision values:
      * ``approved`` -- Reflow resumes processing, status moves to
        ``processing_queued`` and then ``processing``.
      * ``denied``  -- Reflow drops the source PDF, status moves to
        ``denied``. No derived files are generated.

    Justification is required (>= 10 chars) for audit-trail
    accountability. Faculty get the same affordance students sometimes
    receive on FERPA reviews: write down why you made the call.
    """
    decision = (decision or "").strip().lower()
    if decision not in ("approved", "denied"):
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approved' or 'denied'",
        )
    justification = (justification or "").strip()
    if len(justification) < 10:
        raise HTTPException(
            status_code=400,
            detail="justification must be at least 10 characters",
        )
    if len(justification) > 1000:
        raise HTTPException(
            status_code=400,
            detail="justification must be at most 1000 characters",
        )

    job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    # Forward the decision to upstream Reflow Core over HTTP. The connector
    # used to call services.approval_service.ApprovalService directly when
    # this code lived inside the monolith; in the connector split, that
    # logic stays in core and we reach it via the documented PII endpoint.
    # Reviewed-by carries the LTI user id, not an email, since the session
    # doesn't guarantee email is present.
    from ..canvas.reflow_client import ReflowApiError, ReflowClient

    reflow = ReflowClient()
    reviewer = sess.user_email or sess.user_id
    try:
        await reflow.submit_pii_decision(
            job_id=job_id,
            decision=decision,
            justification=justification,
            reviewed_by=reviewer,
        )
    except ReflowApiError as exc:
        # 409: job already advanced past awaiting_approval (race with another
        # instructor approving in a parallel tab). 404: running Reflow Core
        # lacks the endpoint yet — operator action: ship the upstream PII PR.
        if exc.status_code == 409:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        logger.warning(
            "Reflow Core rejected PII decision for job=%s: status=%s msg=%s",
            job_id, exc.status_code, exc.message,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Reflow Core PII decision error: {exc.message}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("PII decision failed for job=%s", job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Reflow Core PII decision error: {exc}",
        ) from exc

    await append_approval_event(
        redis,
        job_id=job_id,
        action="pii_approve" if decision == "approved" else "pii_deny",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=justification,
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.info(
        "PII %s for job=%s by user=%s course=%s",
        decision, job_id, sess.user_id, sess.course_id,
    )
    return JSONResponse(
        {
            "ok": True,
            "decision": decision,
            "next_status": "processing_queued" if decision == "approved" else "denied",
        }
    )


@router.post("/convert/{file_id}")
async def convert_file(
    file_id: str,
    request: Request,
    course_id: str = Query(...),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Queue a file for accessible-page conversion from the overlay.

    Clears the watcher's "already processed" marker so the next watcher tick
    re-downloads and converts the file; the bridge then creates (or updates)
    its Canvas Page. Lets faculty trigger conversion from the UI instead of
    waiting for discovery or running a script. Instructor-only + CSRF, scoped
    to the caller's own course.
    """
    if not session_id:
        raise HTTPException(status_code=401, detail="No LTI session")
    sess = await get_session(redis, session_id)
    if sess is None:
        raise HTTPException(status_code=401, detail="Expired LTI session")
    is_instructor = any(
        r.endswith("Instructor")
        or r.endswith("Administrator")
        or "TeachingAssistant" in r
        or "Admin" in r
        for r in (sess.roles or [])
    )
    if not is_instructor:
        raise HTTPException(status_code=403, detail="Instructor role required")
    if str(sess.course_id) != str(course_id):
        raise HTTPException(status_code=403, detail="Session not bound to this course")
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    # Prefer (re)building the page from an EXISTING completed conversion. These
    # files usually already have a Reflow job (they show a score) — they just
    # never got a Canvas Page (page creation was skipped while OAuth was
    # unconfigured, leaving the job in awaiting_review with no page, which the
    # bridge then never retries). Resetting the job to ``processing`` makes the
    # bridge re-drive it on its next tick (~30s) and build the page from the
    # markdown that already exists — no slow re-run of the AI pipeline.
    # Only when there's no job at all do we queue a fresh conversion.
    job_id = await _lookup_job_for_file(redis, course_id, file_id)
    if job_id:
        job = await get_job(redis, job_id)
        if job is not None and job.status != "processing":
            job.status = "processing"
            await put_job(redis, job)
            logger.info(
                "Manual page-build queued via overlay: course=%s file=%s job=%s by user=%s",
                course_id, file_id, job_id, sess.user_id,
            )
            return JSONResponse({
                "ok": True,
                "message": "Building the accessible page — it'll appear in under a minute.",
            })
        if job is not None:
            return JSONResponse({
                "ok": True,
                "message": "Already building — the page will appear shortly.",
            })

    await clear_processed(redis, course_id, file_id)
    logger.info(
        "Manual convert queued via overlay (no existing job): course=%s file=%s by user=%s",
        course_id, file_id, sess.user_id,
    )
    return JSONResponse({
        "ok": True,
        "message": "Conversion queued — the accessible page will appear in a few minutes.",
    })


@router.post("/approve/{job_id}")
async def approve_job(
    job_id: str,
    request: Request,
    comment: str | None = Body(default=None, embed=True),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Mark a job as published — alt formats become visible to students.

    Only callable by instructors in the same course. Transition is allowed
    from any non-terminal state (typically ``awaiting_review`` after the
    faculty member reviewed/edited the HTML; sometimes ``rejected`` if they
    change their mind). Requires CSRF token (Phase 3).
    """
    job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)
    if job.status == "failed":
        raise HTTPException(status_code=409, detail="Cannot approve a failed job")

    # Phase 7b publication gate. Two things must hold to publish:
    #
    #   1. Automated WCAG checks find no ``error``-severity issues
    #      against the rendered HTML, OR the reviewer has explicitly
    #      waived each error with a justification (``waivers`` body).
    #   2. The reviewer checklist (``checklist`` body) confirms each
    #      manual item the gate requires -- headings, reading order,
    #      alt text, tables, math, etc.
    #
    # Both are passed in the JSON body alongside ``comment``. When
    # omitted (legacy callers / smoke tests) the gate falls open with
    # a logged warning. Production deployments should set
    # ``REQUIRE_WCAG_GATE=true`` (default in prod build) which causes
    # missing/failing checks to 409 instead.
    require_gate = bool(getattr(settings, "require_wcag_gate", False))
    raw_body = await _read_json_body(request)
    waivers: list[str] = list(raw_body.get("waivers") or [])
    checklist: dict[str, bool] = dict(raw_body.get("checklist") or {})

    try:
        rendered = await _resolve_html(redis, job_id)
        wcag = run_wcag_checks(html_full_document(rendered, mathjax=False))
    except Exception as exc:
        logger.exception("approve_job: WCAG check failed for %s", job_id)
        if require_gate:
            raise HTTPException(
                status_code=502,
                detail=f"Could not run WCAG checks: {exc}",
            ) from exc
        wcag = None

    if wcag is not None:
        unwaived = [f for f in wcag.findings
                    if f.severity == "error" and f.rule_id not in waivers]
        if unwaived and require_gate:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "wcag_gate_blocked",
                    "message": (
                        f"{len(unwaived)} WCAG error(s) remain unwaived. "
                        "Fix them or include rule_ids in the 'waivers' "
                        "list with a justification."
                    ),
                    "findings": [asdict(f) for f in unwaived],
                },
            )

    if require_gate:
        required_items = ("headings", "alt_text", "tables", "reading_order")
        missing = [k for k in required_items if not checklist.get(k)]
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "checklist_incomplete",
                    "message": "Reviewer checklist not complete",
                    "missing": missing,
                },
            )

    job.status = "published"
    await put_job(redis, job)
    # Publish the Canvas wiki Page itself (the bridge creates it unpublished),
    # so students can reach it in the course Pages list — not only via the
    # overlay's alt link. Best-effort: the job is published for overlay/alt
    # access either way. Uses the course-owner OAuth client, same as the bridge.
    if job.canvas_page_url or job.canvas_page_id:
        try:
            from ..workers.reflow_bridge_worker import _canvas_client_for_job
            _canvas = await _canvas_client_for_job(redis, job)
            await _canvas.publish_page(
                job.canvas_course_id, job.canvas_page_url or job.canvas_page_id or "",
            )
        except Exception:  # noqa: BLE001 — page publish is best-effort
            logger.exception("approve_job: could not publish Canvas page for %s", job_id)
    await append_approval_event(
        redis,
        job_id=job_id,
        action="approve",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=comment,
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.info("Job %s approved by %s", job_id, sess.user_id)
    return JSONResponse({"ok": True, "status": "published"})


@router.post("/reject/{job_id}")
async def reject_job(
    job_id: str,
    request: Request,
    comment: str | None = Body(default=None, embed=True),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Mark a job as rejected — alt formats stay hidden from students."""
    job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)
    job.status = "rejected"
    await put_job(redis, job)
    await append_approval_event(
        redis,
        job_id=job_id,
        action="reject",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=comment,
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.info("Job %s rejected by %s", job_id, sess.user_id)
    return JSONResponse({"ok": True, "status": "rejected"})


@router.post("/request-edits/{job_id}")
async def request_edits(
    job_id: str,
    request: Request,
    comment: str | None = Body(default=None, embed=True),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Pull a published or rejected job back to awaiting_review so faculty
    can iterate. Useful when faculty changed their mind or wants to edit
    the HTML after publishing."""
    job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)
    job.status = "awaiting_review"
    await put_job(redis, job)
    await append_approval_event(
        redis,
        job_id=job_id,
        action="request_edits",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=comment,
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.info("Job %s pulled back to draft by %s", job_id, sess.user_id)
    return JSONResponse({"ok": True, "status": "awaiting_review"})


@router.post("/unpublish/{job_id}")
async def unpublish_job(
    job_id: str,
    request: Request,
    comment: str | None = Body(default=None, embed=True),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Take a published accessible version down from students.

    Unlike ``request-edits`` (which is about iterating on the content),
    Unpublish is an explicit "remove this from students" action: it returns
    the job to ``awaiting_review`` (so the alt formats are gated again) AND
    unpublishes the Canvas wiki Page itself, so it also disappears from the
    course Pages list. Re-publishing is one click ("Approve & publish"),
    which re-publishes the Page too. Instructor + CSRF + trusted origin.
    """
    job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    job.status = "awaiting_review"
    await put_job(redis, job)
    # Best-effort: also unpublish the Canvas wiki Page so it leaves the
    # student-visible Pages list, not just the gated alt endpoint. Uses the
    # course-owner OAuth client, same as approve/publish.
    if job.canvas_page_url or job.canvas_page_id:
        try:
            from ..workers.reflow_bridge_worker import _canvas_client_for_job
            _canvas = await _canvas_client_for_job(redis, job)
            await _canvas.unpublish_page(
                job.canvas_course_id, job.canvas_page_url or job.canvas_page_id or "",
            )
        except Exception:  # noqa: BLE001 — page unpublish is best-effort
            logger.exception("unpublish_job: could not unpublish Canvas page for %s", job_id)
    await append_approval_event(
        redis,
        job_id=job_id,
        action="unpublish",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=comment,
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.info("Job %s unpublished by %s", job_id, sess.user_id)
    return JSONResponse({"ok": True, "status": "awaiting_review"})


@router.get("/audit/approvals.csv")
async def approvals_audit_csv(
    since: float | None = Query(default=None),
    redis: Redis = Depends(get_redis_client),
) -> Response:
    """Return a CSV of every approve/reject/request_edits/pii_approve event.

    No auth gate here: this endpoint is intended for ops/SRE use behind
    an internal reverse proxy. In production deploy this behind a
    network ACL or IP allowlist.
    """
    import csv
    import io

    events = await list_approval_events(redis, since=since)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "at", "job_id", "action", "actor_user_id", "actor_name",
        "actor_ip", "course_id", "comment",
    ])
    for e in events:
        writer.writerow([
            e.get("at", ""),
            e.get("job_id", ""),
            e.get("action", ""),
            e.get("actor_user_id", ""),
            e.get("actor_name", ""),
            e.get("actor_ip", ""),
            e.get("course_id", ""),
            (e.get("comment") or "").replace("\n", " ")[:500],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="approvals.csv"',
        },
    )


class _BulkApproveBody(BaseModel):  # type: ignore[name-defined]
    job_ids: list[str]
    comment: str | None = None


@router.post("/approve/_bulk")
async def approve_bulk(
    request: Request,
    body: _BulkApproveBody,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Approve many jobs at once. Skips jobs in 'failed' state.

    All jobs must belong to the caller's course (enforced via the same
    ``_require_instructor`` check used by the per-job endpoint). On
    any auth failure the whole call rejects — no partial commit.
    """
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)
    if not body.job_ids:
        raise HTTPException(status_code=400, detail="job_ids must be non-empty")
    if len(body.job_ids) > 200:
        raise HTTPException(status_code=400, detail="Too many job_ids; cap is 200")

    approved: list[str] = []
    skipped: list[dict[str, Any]] = []
    for jid in body.job_ids:
        try:
            job, sess = await _require_instructor(redis, session_id, jid)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise  # session/role failure aborts the batch
            skipped.append({"job_id": jid, "reason": str(exc.detail)})
            continue
        if job.status == "failed":
            skipped.append({"job_id": jid, "reason": "failed jobs are not approvable"})
            continue
        job.status = "published"
        await put_job(redis, job)
        await append_approval_event(
            redis,
            job_id=jid,
            action="approve",
            actor_user_id=sess.user_id,
            actor_name=sess.user_name,
            comment=body.comment,
            actor_ip=_client_ip(request),
            course_id=sess.course_id,
        )
        approved.append(jid)
    logger.info("Bulk approve: %d approved, %d skipped", len(approved), len(skipped))
    return JSONResponse({"approved": approved, "skipped": skipped})


@router.delete("/job/{job_id}")
async def delete_job(
    job_id: str,
    request: Request,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Phase 11: hard-delete a job and every derivative artifact.

    Requires instructor (course-bound) AND CSRF token AND trusted
    origin. The deletion is irreversible from a UI standpoint --
    audit-log entries are preserved (per institutional record-keeping
    requirements) but every artifact (source PDF reference, markdown,
    figures, edited HTML, score cache, approval token) is removed.

    S3 cleanup is not performed inline; a separate sweeper reconciles
    orphaned S3 keys against the Redis index on a schedule.
    """
    _job, sess = await _require_instructor(redis, session_id, job_id)
    _require_csrf(session_id, csrf_token)
    _require_trusted_origin(request)

    from ..canvas.privacy import delete_job_and_derivatives
    summary = await delete_job_and_derivatives(redis, reflow_job_id=job_id)

    await append_approval_event(
        redis,
        job_id=job_id,
        action="delete",
        actor_user_id=sess.user_id,
        actor_name=sess.user_name,
        comment=f"Hard-deleted: {summary}",
        actor_ip=_client_ip(request),
        course_id=sess.course_id,
    )
    logger.warning(
        "Job %s deleted by user=%s course=%s summary=%s",
        job_id, sess.user_id, sess.course_id, summary,
    )
    return JSONResponse({"ok": True, "deleted": summary})
