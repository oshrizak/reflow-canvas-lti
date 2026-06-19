"""Bridge worker — turns completed Reflow jobs into draft Canvas Pages.

Listens for completion events from Reflow, fetches the result markdown,
renders it to HTML, creates an unpublished Canvas Page, and notifies the
professor via a Canvas Conversation.

Why a separate worker from the watcher: separation of concerns and
independent retry/backoff. The watcher's job is to start work; this
worker's job is to finish it.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from redis.asyncio import Redis

from ..canvas.client import CanvasClient
from ..canvas.errors import CanvasApiError
from ..canvas.markdown_to_html import render, render_link_stub
from ..canvas.reflow_client import ReflowClient, rewrite_presigned_url
from ..canvas.state import (
    CanvasJob,
    get_file_page,
    get_job,
    put_file_page,
    put_job,
)
from ..canvas.tenant import tk
from ..config import settings
from ..lti.platform_store import (
    get_course_owner,
    get_platform,
    get_platform_for_course,
)

# Scopes the bridge needs to create + publish a Canvas Page and notify
# the uploader. Strict subset of /lti/config.json so an admin who
# approved the full set already covers this.
# Course folder where converted-document figures are uploaded. Appears in
# the course Files list; faculty can lock/hide it if they prefer. Keeping a
# single named folder keeps the generated images tidy and easy to find.
_FIGURE_FOLDER = "Reflow Generated Images"


def _slugify(title: str) -> str:
    """Approximate Canvas's wiki-page slug derivation from a title.

    Lowercase, runs of non-alphanumerics -> single hyphen, trim hyphens.
    Used as a fallback page reference so the first update-in-place run
    reuses the existing page instead of creating yet another duplicate.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "page"

_BRIDGE_SCOPES = [
    "url:POST|/api/v1/courses/:course_id/pages",
    "url:PUT|/api/v1/courses/:course_id/pages/:url_or_id",
    "url:POST|/api/v1/conversations",
    "url:POST|/api/v1/courses/:course_id/files",
]


async def _canvas_client_for_job(redis, job: CanvasJob) -> CanvasClient:
    """Build the right Canvas client for a job.

    Priority order (best-token-first):

      1. **User-OAuth path** -- the job has a ``canvas_user_id`` AND we
         have stored OAuth credentials for that user on this platform.
         This is the only auth that works for the general Canvas REST
         API in Canvas Cloud, and it authors the Page as that instructor.
         Refreshes silently on 401.
      2. **Service-token path** -- known platform but no user token.
         Useful for LTI Advantage services (NRPS, AGS); does NOT work
         for ``/api/v1/...`` calls in Canvas Cloud, so the bridge will
         see 401s here. Logged as a warning.
      3. **Env-token fallback** -- no platform association or no stored
         credentials. Requires ``CANVAS_API_TOKEN`` to be set; raises
         otherwise.
    """
    from ..canvas.user_oauth import get_user_token

    platform_id = job.platform_id
    if not platform_id and job.canvas_course_id:
        platform_id = await get_platform_for_course(redis, job.canvas_course_id)

    if platform_id:
        platform = await get_platform(redis, platform_id)
        if platform is not None:
            # 1. Prefer the job's own uploader token when one is stored.
            if job.canvas_user_id:
                try:
                    user_token = await get_user_token(
                        redis, platform.platform_id, job.canvas_user_id,
                    )
                except Exception:
                    user_token = None
                if user_token is not None:
                    return CanvasClient.from_user_token(
                        redis, platform, job.canvas_user_id,
                    )
            # 2. Fall back to the COURSE OWNER's OAuth token. The job's
            #    canvas_user_id is often the file's uploader (or a Canvas
            #    numeric id) which has no stored token, whereas the course
            #    owner is the instructor who actually completed the OAuth
            #    consent — their token is valid and is what the watcher uses
            #    to read the course. Without this, the bridge drops to the
            #    LTI service token, which Canvas rejects with invalid_scope
            #    for page/file writes (it can't carry url:POST scopes).
            owner_id = await get_course_owner(redis, job.canvas_course_id)
            if owner_id and owner_id != job.canvas_user_id:
                try:
                    owner_token = await get_user_token(
                        redis, platform.platform_id, owner_id,
                    )
                except Exception:
                    owner_token = None
                if owner_token is not None:
                    logger.info(
                        "Job %s: using course-owner OAuth token (owner=%s); "
                        "job uploader=%r had no stored token",
                        job.reflow_job_id, owner_id, job.canvas_user_id,
                    )
                    return CanvasClient.from_user_token(redis, platform, owner_id)
            # 3. No usable user token: fall back to service token. Works for
            #    LTI Advantage svcs but 401s/invalid_scopes on REST writes.
            logger.warning(
                "Job %s has no stored user token (uploader=%r, owner=%r); "
                "falling back to LTI Advantage service token (page/file "
                "writes will fail with invalid_scope)",
                job.reflow_job_id, job.canvas_user_id, owner_id,
            )
            return CanvasClient.from_platform(redis, platform, _BRIDGE_SCOPES)
        logger.warning(
            "Job %s references unknown platform_id=%r; falling back to env token",
            job.reflow_job_id, platform_id,
        )
    return CanvasClient()

logger = logging.getLogger(__name__)


async def start_reflow_bridge(
    redis: Redis,
    *,
    shutdown_event: asyncio.Event,
    poll_interval_seconds: int | None = None,
) -> None:
    """Long-running task: drive in-flight jobs to completion.

    The MVP polls per-job status rather than subscribing to a global event
    stream — simpler, and the working set is small (one course's pending
    uploads). The SSE path lives behind ``stream_events`` on the Reflow
    client and is wired up in Phase 4.
    """

    interval = poll_interval_seconds or int(getattr(settings, "reflow_poll_seconds", 30))
    reflow = ReflowClient()
    # The bridge used to share one CanvasClient process-wide. In
    # multi-tenant mode each job may belong to a different Canvas
    # instance, so we construct per-job inside _drive_job instead.
    # ``canvas`` here is None; downstream code asks _canvas_client_for_job
    # for the right client when it needs to make a Canvas call.
    canvas: CanvasClient | None = None

    while not shutdown_event.is_set():
        try:
            await _tick(redis, reflow, canvas)
        except Exception:  # pragma: no cover — defensive
            logger.exception("Reflow bridge tick failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except TimeoutError:
            continue


# Canvas-side states the bridge is allowed to poll/update. We
# DELIBERATELY include ``awaiting_review`` and ``failed`` so the
# bridge can correct stale state when Reflow disagrees later (e.g.
# a late-arriving completion, a timeout-induced failure, or an ops
# replay of a stuck job). ``page_failed`` is included so the bridge
# keeps retrying the Canvas Page write on each tick — the conversion
# already succeeded, so once the OAuth token gains the Pages scope the
# page builds itself with no manual replay. The two we never touch are
# ``published`` (faculty already approved — overwriting would lose their
# decision) and ``rejected`` (faculty already declined — same rationale).
_BRIDGE_POLLABLE: set[str] = {
    "processing", "awaiting_review", "failed", "page_failed", "awaiting_approval",
}

# Hard ceiling on how long a single job may occupy the tick. The bridge's
# job is to reflect Reflow status + (re)build one Canvas Page from an
# ALREADY-converted document: fetch status, download a handful of figures,
# upload them, and create/update one page. That should take seconds. If any
# single network call wedges (a Canvas/S3 request with no effective timeout),
# the whole sequential tick would otherwise block forever and strand every
# job after it in 'processing'. Cap each job so the scan always moves on.
_DRIVE_JOB_TIMEOUT_S = 180


async def _tick(redis: Redis, reflow: ReflowClient, canvas: CanvasClient) -> None:
    """One pass: reflect Reflow's view back onto every non-terminal canvas job.

    Bug history: the previous filter (``job.status != "processing"``)
    meant the bridge stopped watching a job the moment it moved out of
    ``processing`` — so a late status change on the Reflow side never
    propagated, and an ops manual replay that succeeded after the
    inline BackgroundTask had crashed left the canvas record stuck at
    ``failed`` forever. Wider filter + bidirectional reflection in
    ``_drive_job`` fixes that.
    """

    # Scan all canvas job keys. For the MVP this is fine; for Phase 6+ we
    # maintain an active-set index to avoid the KEYS scan.
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=tk("canvas:job:*"), count=100)
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            job_id = key.rsplit(":", 1)[-1]
            job = await get_job(redis, job_id)
            if job is None or job.status not in _BRIDGE_POLLABLE:
                continue
            try:
                job_canvas = await _canvas_client_for_job(redis, job)
            except Exception:
                logger.exception(
                    "Could not build Canvas client for job %s; will retry next tick",
                    job.reflow_job_id,
                )
                continue
            # Isolate each job: a hang times out, an error is logged, and the
            # scan continues to the next job either way. Without this, one bad
            # job stalls or aborts the whole tick and strands every job after
            # it in 'processing' (observed: 4 pages rebuilt, then a 20-min hang).
            try:
                await asyncio.wait_for(
                    _drive_job(redis, reflow, job_canvas, job),
                    timeout=_DRIVE_JOB_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "Bridge: job %s exceeded %ss; moving on (will retry next tick)",
                    job.reflow_job_id, _DRIVE_JOB_TIMEOUT_S,
                )
            except Exception:
                logger.exception(
                    "Bridge: job %s failed to drive; moving on", job.reflow_job_id,
                )
        if cursor == 0:
            break


# ---------------------------------------------------------------------------
# Helpers used by ``_drive_job`` to build the Canvas Page artifact.
#
# These were lost in the transcript-based reconstruction of this file and
# restored here with intentionally minimal behavior: do the obvious thing,
# fail soft, and never block the job from advancing to ``awaiting_review``.
# ---------------------------------------------------------------------------

def _title_from_filename(filename: str | None) -> str:
    """Clean filename into a human-friendly Canvas Page title.

    Strips the source extension (.pdf / .docx / etc.) and trims
    whitespace. Falls back to ``"Document"`` when filename is empty.
    """
    if not filename:
        return "Document"
    name = str(filename).strip()
    for ext in (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".html", ".htm", ".epub"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name.strip() or "Document"


def _canvas_file_url(job: CanvasJob) -> str | None:
    """Best-effort URL to the original source file in Canvas.

    The bridge worker doesn't always know the institutional host
    (e.g. the legacy single-tenant path doesn't carry a platform).
    When we can't build a confident URL we return ``None`` -- the
    renderer treats ``None`` as "no source link" and just omits it
    from the rendered HTML.
    """
    course_id = getattr(job, "canvas_course_id", None)
    file_id = getattr(job, "canvas_file_id", None)
    if not course_id or not file_id:
        return None
    # Build a path-only link; the panorama overlay rewrites these in
    # the browser using ``window.location.origin``, which is exactly
    # the Canvas host the user is currently on. That way the same
    # rendered HTML works for any institution it's served to.
    return f"/courses/{course_id}/files/{file_id}"


async def _notify_professor(canvas: CanvasClient, job: CanvasJob) -> None:
    """Best-effort: drop a Canvas Conversation announcing the new draft.

    We try once, log on failure, and move on. The faculty member
    will see the dial in the panorama overlay anyway -- this
    notification is just a courtesy when their inbox is where they
    notice things.

    NOTE: this is a stub. The historical implementation called
    ``canvas.create_conversation(...)`` against a small set of
    recipients. After the file-recovery regression that original
    body was lost; the simplest no-regression behavior is to log a
    debug line and skip until the helper is re-fleshed.
    """
    try:
        recipient = getattr(job, "canvas_user_id", "") or "(unknown)"
        logger.debug(
            "Bridge: would notify professor user=%s about job=%s "
            "(notification helper is a stub; faculty will still see "
            "the dial in the panorama overlay)",
            recipient, job.reflow_job_id,
        )
    except Exception:  # pragma: no cover -- belt + suspenders
        logger.exception("notify_professor stub raised; ignoring")


async def _drive_job(
    redis: Redis,
    reflow: ReflowClient,
    canvas: CanvasClient,
    job: CanvasJob,
) -> None:
    """Reflect Reflow's current status onto the canvas job record.

    Allowed transitions, by current canvas state:
      * canvas=processing/failed + reflow=completed -> canvas=awaiting_review
        (continues into the Page + notify block below)
      * canvas=processing/awaiting_review + reflow=failed/denied -> canvas=failed
      * canvas=awaiting_review + reflow=completed -> no-op (already there)
      * canvas=failed + reflow still in-flight -> no-op (give it time)

    We intentionally never overwrite ``published`` or ``rejected`` —
    those are human-decided terminal states, see ``_BRIDGE_POLLABLE``.
    """
    status = await reflow.get_status(job.reflow_job_id)
    state = (status.get("status") or "").lower()

    if state in {"failed", "denied"}:
        if job.status != "failed":
            prior = job.status
            job.status = "failed"
            job.error = status.get("error", f"Reflow status: {state}")
            await put_job(redis, job)
            logger.warning(
                "Bridge: job %s flipped %s->failed (reflow=%s, err=%s)",
                job.reflow_job_id, prior, state, job.error,
            )
        return

    if state == "completed":
        # No-op only if a Canvas Page actually exists. A job stranded at
        # awaiting_review with no page id/url is a victim of the old
        # "best-effort page write" behavior (the write failed but the status
        # advanced anyway), so faculty saw a clean dial but students got no
        # page. Fall through and (re)build it — this self-heals those legacy
        # jobs on the next tick once Pages access is in place, with no manual
        # replay. A genuinely-built awaiting_review job keeps its page id and
        # is left alone (faculty owns the next transition).
        if job.status == "awaiting_review" and (job.canvas_page_id or job.canvas_page_url):
            return
        # Otherwise fall through to the completion block which moves
        # canvas to awaiting_review and tries to create the Page.
    elif state == "awaiting_approval":
        # Reflow paused for a PII/privacy decision. Reflect that onto the
        # canvas status instead of leaving it 'processing' — otherwise the
        # timeout watchdog mislabels it "stuck in processing for >3600s" and
        # tells faculty to re-upload, when the real action is a one-click
        # Approve/Deny. The overlay shows that prompt for this status. We keep
        # polling (it's in _BRIDGE_POLLABLE), so once approved Reflow resumes
        # to completed and the next tick builds the page.
        if job.status != "awaiting_approval":
            job.status = "awaiting_approval"
            job.error = None
            await put_job(redis, job)
        return
    else:
        # Reflow is still working (pii_scanning / processing*); nothing to write.
        return

    # The completed status payload carries the result URLs directly —
    # there is no separate /result endpoint on the Reflow API.
    markdown_url = status.get("markdown_url") or status.get("result_url")
    if not markdown_url:
        logger.warning("Reflow job %s completed without markdown_url", job.reflow_job_id)
        return
    markdown = await reflow.fetch_markdown(markdown_url)

    # Derive per-document conversion-quality signals from the markdown
    # we just received. These replace the (always-empty) ``signals``
    # field upstream Reflow doesn't populate and give the panorama
    # dial something honest to display.
    try:
        from ..canvas.signals import derive_signals_from_markdown
        job.signals = derive_signals_from_markdown(
            markdown,
            pdf_classification=status.get("pdf_classification"),
            ocr_was_run=bool(status.get("ocr_applied") or status.get("ocr_was_run")),
        )
        logger.info(
            "Bridge: derived signals for job=%s (headings=%d, images=%d/%d, tables=%d)",
            job.reflow_job_id,
            len(job.signals.get("heading_levels") or []),
            job.signals.get("images_with_alt") or 0,
            job.signals.get("images_total") or 0,
            job.signals.get("tables_total") or 0,
        )
    except Exception:  # noqa: BLE001 — signal derivation is best-effort
        logger.exception("Bridge: signal derivation failed for %s", job.reflow_job_id)
        job.signals = None

    # Figures land in S3 with short-lived presigned URLs that expire, so we
    # can't embed them in a permanent Canvas Page. Instead, point every
    # ``<img>`` at our stable figure-proxy route, which regenerates S3 access
    # server-side on each request. The Canvas Page is served from Canvas's
    # origin, so the src must be ABSOLUTE (use LTI_PUBLIC_URL).
    # Make the Canvas Page self-contained: upload each figure into a course
    # folder and rewrite the markdown's relative ``figures/<id>.png`` refs to
    # Canvas's own file URLs. This decouples the page from the tunnel and the
    # Reflow backend -- it keeps rendering even if either is down (the old
    # figure-proxy approach broke whenever the public hostname rotated).
    # Reflow serializes figures under ``figures`` (list of {figure_id, url});
    # ``stored_figures`` is the internal Redis field, not in the API response.
    figures = status.get("figures") or status.get("stored_figures") or []
    # Seed from any figures we already uploaded on a prior tick (a page_failed
    # retry). Reusing the stored Canvas URLs means a job that keeps retrying the
    # page write doesn't re-download from S3 and re-POST to Canvas every ~30s.
    figure_canvas_urls: dict[str, str] = dict(getattr(job, "figure_canvas_urls", None) or {})

    # Fetch the source PDF once so we can pull figure bytes directly from
    # it — Reflow's S3 copies carry a vision-pipeline tile grid baked in,
    # while the PDF's own embedded rasters are the original clean
    # imagery. The download is gated on there being at least one figure
    # that isn't already in figure_canvas_urls (otherwise the existing
    # map covers everything and the PDF fetch is wasted work).
    needs_pdf = any(
        f"figures/{str(f.get('figure_id') or '').strip()}.png" not in figure_canvas_urls
        for f in figures
        if str(f.get("figure_id") or "").strip()
    )
    pdf_bytes: bytes | None = None
    if needs_pdf and figures:
        try:
            pdf_bytes = await canvas.download_file(job.canvas_file_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Bridge: PDF download failed for figure extraction (job %s, file %s)",
                job.reflow_job_id, job.canvas_file_id,
            )

    for fig in figures:
        fid = str(fig.get("figure_id") or "").strip()
        src = str(fig.get("url") or "").strip()
        if not fid or not src:
            continue
        ref = f"figures/{fid}.png"
        if ref in figure_canvas_urls:
            # Already uploaded on an earlier tick — just rewrite the markdown.
            markdown = markdown.replace(ref, figure_canvas_urls[ref])
            continue
        # Reflow numbers figures PER DOCUMENT ("figure-1", "figure-2", ...), so
        # the bare "{fid}.png" name collides across files in the shared folder.
        # With Canvas's on_duplicate=overwrite, every PDF's "figure-1.png"
        # overwrites the previous one and they all resolve to whichever file
        # uploaded last -- images bleed across pages. Prefix with the stable
        # Canvas source-file id: unique across files, stable across re-converts
        # (so re-running a single file still overwrites its own figures, not a
        # neighbour's). ``ref`` stays the per-document markdown key.
        canvas_fig_name = f"{job.canvas_file_id}-{fid}.png"
        try:
            # Prefer the PDF-extracted bytes (no vision-pipeline grid).
            # Fall back to Reflow's S3 PNG when the figure isn't an
            # embedded raster (e.g., a vector chart) — better to ship
            # the gridded copy than to drop the figure entirely.
            figure_bytes: bytes | None = None
            content_type = "image/png"
            if pdf_bytes is not None:
                from ..canvas.pdf_figures import (
                    PdfFigureNotFound,
                    extract_figure_for_reflow_id,
                )
                try:
                    extracted = extract_figure_for_reflow_id(pdf_bytes, figures, fid)
                    figure_bytes = extracted.image_bytes
                    content_type = extracted.content_type
                except PdfFigureNotFound as exc:
                    logger.info(
                        "Bridge: PDF extraction skipped for job %s fig %s: %s",
                        job.reflow_job_id, fid, exc,
                    )
            if figure_bytes is None:
                fetch_url = rewrite_presigned_url(src)
                async with httpx.AsyncClient(timeout=60.0) as hc:
                    img_resp = await hc.get(fetch_url, follow_redirects=True)
                img_resp.raise_for_status()
                figure_bytes = img_resp.content

            uploaded = await canvas.upload_course_file(
                job.canvas_course_id,
                canvas_fig_name,
                figure_bytes,
                content_type=content_type,
                folder_path=_FIGURE_FOLDER,
            )
            canvas_url = str(uploaded.get("url") or "")
            if canvas_url:
                figure_canvas_urls[ref] = canvas_url
                markdown = markdown.replace(ref, canvas_url)
        except Exception:  # noqa: BLE001 — one bad figure shouldn't sink the page
            logger.exception(
                "Bridge: failed to upload figure %s to Canvas for job %s",
                fid, job.reflow_job_id,
            )
    if figure_canvas_urls:
        # Persist the map so the overlay's accessible-HTML views embed the
        # same Canvas-hosted images (they have no proxy to fall back on).
        job.figure_canvas_urls = figure_canvas_urls
        logger.info(
            "Bridge: uploaded %d/%d figures to Canvas folder %r for job %s",
            len(figure_canvas_urls), len(figures), _FIGURE_FOLDER, job.reflow_job_id,
        )

    title = _title_from_filename(job.canvas_file_name)

    # ``image_base_url=None``: figure refs are already absolute Canvas URLs
    # after the rewrite above, so no further path rewriting is needed.
    rendered = render(
        markdown,
        title=title,
        image_base_url=None,
        original_pdf_url=_canvas_file_url(job),
    )

    # Two Canvas Cloud constraints rule out putting the document IN the Page:
    #   1. the edge WAF rejects Page REST writes whose body exceeds ~8KB, and
    #   2. Canvas won't render an uploaded .html file inline (it only offers a
    #      download — confirmed: the file preview shows a Canvas 404).
    # So the Page is a small stub that links to the tool-served rendered HTML
    # (/canvas/panorama/alt/{job}/html) — the same surface the overlay's
    # "Accessible HTML" button uses, which renders reliably in any environment.
    # The stub is always tiny, so the page write clears the WAF at any document
    # size. Faculty must publish the job before students can open the link;
    # until then it shows a "pending review" message — the intended approval
    # gate. See the canvas-waf-page-body-limit note.
    public_base = (getattr(settings, "lti_public_url", "") or "").rstrip("/")
    accessible_url = f"{public_base}/canvas/panorama/alt/{job.reflow_job_id}/html"
    page_body = render_link_stub(
        title=rendered.title,
        accessible_url=accessible_url,
        original_pdf_url=_canvas_file_url(job),
    )

    # Reuse ONE stable page per file. The bridge used to POST a new page on
    # every (re)conversion, so Canvas accumulated duplicate "…-2/-3/…" pages.
    # Now we look up the slug we created for this file last time and PUT-update
    # it; we only create when none exists (or the update 404s). ``page_ref``
    # falls back to the deterministic slug Canvas derives from the title, so
    # the first run after this change reuses the original page instead of
    # adding yet another duplicate. If the token lacks manage_wiki (401/403),
    # we mark page_failed below rather than faking awaiting_review.
    page_ref = await get_file_page(redis, job.canvas_course_id, job.canvas_file_id)
    if not page_ref:
        page_ref = _slugify(rendered.title)
    try:
        try:
            page = await canvas.update_page(
                job.canvas_course_id, page_ref, rendered.title, page_body,
            )
        except CanvasApiError as exc:
            if exc.status_code == 404:
                page = await canvas.create_page(
                    job.canvas_course_id,
                    rendered.title,
                    page_body,
                    published=False,
                )
            else:
                raise
        job.canvas_page_id = str(page.get("page_id", ""))
        # Store the full, openable page URL (``html_url``) for the overlay's
        # "Open accessible Canvas page" link. Canvas's ``url`` field is only
        # the bare slug, which rendered as a link 404s (it resolves to
        # /courses/<id>/<slug> instead of /courses/<id>/pages/<slug>).
        # publish_page/delete_page normalize whichever form they're given.
        job.canvas_page_url = str(page.get("html_url") or page.get("url") or "")
        # Remember the slug so the next re-conversion updates this same page.
        await put_file_page(
            redis,
            job.canvas_course_id,
            job.canvas_file_id,
            str(page.get("url") or job.canvas_page_url),
        )
    except CanvasApiError as exc:
        if exc.status_code in (401, 403):
            # The conversion succeeded but Canvas rejected the page write —
            # almost always the course-owner OAuth token missing the Pages
            # write scope (url:POST|/api/v1/courses/:course_id/pages). DON'T
            # advance to awaiting_review: that strands the job claiming success
            # with no page ever created (the "pages never got made" bug). Mark
            # it page_failed so the overlay shows the real reason; page_failed
            # is pollable, so the next tick retries and the page self-heals once
            # the scope is granted — no manual replay needed.
            logger.warning(
                "Canvas page write FAILED for job %s (status=%d): %s. "
                "Marking page_failed — grant the OAuth token the Pages write "
                "scope (url:POST|/api/v1/courses/:course_id/pages) and it will "
                "rebuild automatically.",
                job.reflow_job_id, exc.status_code, exc.message,
            )
            job.canvas_page_id = ""
            job.canvas_page_url = ""
            job.status = "page_failed"
            job.error = f"Canvas page write {exc.status_code}: {exc.message}"
            await put_job(redis, job)
            return
        raise

    job.status = "awaiting_review"
    job.error = None  # clear any prior page_failed error now that the page exists
    await put_job(redis, job)

    # Notify the uploader only when we have a Canvas Page to point at;
    # otherwise the Conversations message would be useless.
    if job.canvas_page_id:
        await _notify_professor(canvas, job)

