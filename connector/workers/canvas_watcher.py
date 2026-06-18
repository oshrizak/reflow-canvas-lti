"""Canvas file-discovery watcher.

Scans every surface a faculty member or student could attach a file to
inside a Canvas course, accumulates a deduplicated set of file ids, then
submits each PDF Reflow hasn't seen before.

Surfaces covered (all on a 60s default poll):
  1. Files page  /courses/:id/files
  2. All folders /courses/:id/folders + /folders/:id/files
     (catches RCE-paperclip uploads that land in hidden internal folders)
  3. Modules     /courses/:id/modules/:id/items where type == "File"
  4. Pages       /courses/:id/pages -> body HTML scanned for file refs
  5. Discussions /courses/:id/discussion_topics + their entries (incl.
     announcements). Both the topic message HTML and each entry's
     message HTML are scanned; entry.attachments[] is also harvested.
  6. Assignments /courses/:id/assignments — description HTML
  7. Quizzes     /courses/:id/quizzes — description HTML
  8. Syllabus    /courses/:id?include[]=syllabus_body

After id collection, each fresh file id is resolved via
``GET /files/:id`` for metadata, filtered to PDFs, and submitted to
Reflow. The processed set in Redis guards against double-submission.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from redis.asyncio import Redis

from ..canvas.client import CanvasClient
from ..canvas.errors import CanvasApiError
from ..canvas.reflow_client import ReflowClient
from ..canvas.spend_cap import reserve_submission
from ..canvas.state import (
    CanvasJob,
    already_processed,
    mark_processed,
    put_job,
)
from ..canvas.tenant import tk
from ..canvas.user_oauth import get_user_token
from ..config import settings
from ..lti.platform import PlatformInstall
from ..lti.platform_store import (
    get_course_owner,
    get_courses_for_platform,
    list_platforms,
)

# Default scope set the watcher's CanvasClient asks for in multi-tenant
# mode. Matches the readable-discovery subset declared in
# /lti/config.json so a Developer Key approved for the full scope list
# can satisfy these requests without surprise.
_WATCHER_SCOPES = [
    "url:GET|/api/v1/courses/:course_id/files",
    "url:GET|/api/v1/courses/:course_id/folders",
    "url:GET|/api/v1/courses/:course_id/modules",
    "url:GET|/api/v1/courses/:course_id/modules/:module_id/items",
    "url:GET|/api/v1/courses/:course_id/pages",
    "url:GET|/api/v1/courses/:course_id/pages/:url_or_id",
    "url:GET|/api/v1/courses/:course_id/discussion_topics",
    "url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries",
    "url:GET|/api/v1/courses/:course_id/assignments",
    "url:GET|/api/v1/courses/:course_id/quizzes",
    "url:GET|/api/v1/files/:id",
    "url:GET|/api/v1/folders/:id/files",
]

logger = logging.getLogger(__name__)

# Regexes that extract Canvas file ids from arbitrary HTML content. Covers
# all the URL shapes Canvas's RCE and APIs produce:
#   - /courses/X/files/Y
#   - /files/Y/preview
#   - /files/Y/download
#   - data-api-endpoint=".../files/Y"
#   - <a class="instructure_file_link" href=".../files/Y/preview">
_FILE_ID_PATTERNS = [
    re.compile(r"/files/(\d+)(?:[/?#\"\\']|$)"),
    re.compile(r"data-api-endpoint=\"[^\"]*/files/(\d+)\""),
]


def _extract_file_ids_from_html(html: str | None) -> set[str]:
    if not html:
        return set()
    ids: set[str] = set()
    for pattern in _FILE_ID_PATTERNS:
        for match in pattern.finditer(html):
            ids.add(match.group(1))
    return ids


# Document types Reflow can convert. Each type is handled by Docling, which is
# format-agnostic — the only thing the watcher needs to do is filter for
# extensions/mime-types that Reflow actually accepts. Extend this set as new
# input formats are wired into the pipeline.
_CONVERTIBLE_EXTS = (".pdf", ".docx", ".pptx")
_CONVERTIBLE_MIMES = {
    "pdf",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
    "doc",
    "application/msword",  # legacy .doc — Docling converts to docx-like via libreoffice
}


def _is_convertible(file_meta: dict[str, Any]) -> bool:
    """True if the file is in a format Reflow can convert to accessible HTML."""
    mime = (file_meta.get("content-type") or file_meta.get("mime_class") or "").lower()
    name = (file_meta.get("display_name") or file_meta.get("filename") or "").lower()
    if mime in _CONVERTIBLE_MIMES:
        return True
    return any(name.endswith(ext) for ext in _CONVERTIBLE_EXTS)


# Kept for backward compatibility; new code should call _is_convertible.
def _is_pdf(file_meta: dict[str, Any]) -> bool:
    return _is_convertible(file_meta)


def _watched_courses() -> list[str]:
    raw = getattr(settings, "canvas_watched_courses", "") or ""
    return [c.strip() for c in str(raw).split(",") if c.strip()]


def _multi_tenant_enabled() -> bool:
    """Feature flag: when on, watcher iterates registered platforms."""
    return bool(getattr(settings, "multi_tenant_watcher", False))


async def _enumerate_scan_targets(
    redis: Redis,
) -> list[tuple[str, PlatformInstall | None]]:
    """Decide what to scan this tick.

    Returns a list of ``(course_id, platform_or_None)`` pairs. In legacy
    single-tenant mode, ``platform`` is ``None`` and the existing
    env-token CanvasClient handles auth. In multi-tenant mode the
    platform record is supplied and the per-platform service-token
    client is used.

    Multi-tenant precedence is intentional: if a course is registered
    under a platform AND named in canvas_watched_courses, the platform
    path wins so the watcher doesn't double-scan with two different
    auth modes.
    """
    if not _multi_tenant_enabled():
        return [(cid, None) for cid in _watched_courses()]

    targets: list[tuple[str, PlatformInstall | None]] = []
    seen: set[tuple[str, str]] = set()
    platforms = await list_platforms(redis)
    for p in platforms:
        if p.revoked_at:
            logger.debug("skipping revoked platform %s", p.platform_id)
            continue
        course_ids = await get_courses_for_platform(redis, p.platform_id)
        for course_id in course_ids:
            key = (p.platform_id, course_id)
            if key in seen:
                continue
            seen.add(key)
            targets.append((course_id, p))

    # Belt-and-suspenders: if any courses were listed in the legacy env
    # var but have no platform association yet (e.g. operator added one
    # for a not-yet-launched-via-LTI course), keep scanning them via the
    # env token so they aren't silently dropped during the migration.
    platform_courses = {cid for cid, _ in targets}
    for cid in _watched_courses():
        if cid not in platform_courses:
            targets.append((cid, None))

    return targets


async def _make_canvas_client(
    redis: Redis,
    platform: PlatformInstall | None,
    course_id: str | None = None,
) -> CanvasClient:
    """Construct the right CanvasClient for a (course, platform) pair.

    Priority (best-auth-first):

      1. **User-OAuth path** -- platform is known AND the course has a
         stored owner AND that owner has a stored user_token. This is
         the only path that actually works against the general Canvas
         REST API in Canvas Cloud. The watcher reads on the faculty
         owner's behalf, so discovered files and any pages the bridge
         creates are attributed to that instructor.
      2. **Service-token path** -- platform known but no owner/token.
         Useful only for LTI Advantage services (NRPS, AGS); will 401
         for general API calls. Logged as a warning.
      3. **Env-token fallback** -- no platform at all (single-tenant
         deployment). Requires CANVAS_API_TOKEN; raises otherwise.
    """
    if platform is None:
        return CanvasClient()
    if course_id:
        owner_id = await get_course_owner(redis, course_id)
        if owner_id:
            try:
                ut = await get_user_token(redis, platform.platform_id, owner_id)
            except Exception:
                ut = None
            if ut is not None:
                # Promoted to INFO: operators want a visible signal that
                # the watcher is using the user-OAuth path (not the
                # legacy env-token fallback). One line per course per
                # scan tick is cheap and very valuable in incident review.
                logger.info(
                    "Watcher scan: course=%s using owner user_token (user_id=%s)",
                    course_id, owner_id,
                )
                return CanvasClient.from_user_token(redis, platform, owner_id)
    logger.warning(
        "Watcher scan: course=%s has no stored owner/user_token; falling "
        "back to LTI Advantage service token (general API calls will 401)",
        course_id,
    )
    return CanvasClient.from_platform(redis, platform, _WATCHER_SCOPES)


async def start_canvas_watcher(
    redis: Redis,
    *,
    shutdown_event: asyncio.Event,
    poll_interval_seconds: int | None = None,
) -> None:
    interval = poll_interval_seconds or int(getattr(settings, "canvas_poll_seconds", 60))
    if _multi_tenant_enabled():
        logger.info("Canvas watcher: multi-tenant mode enabled")
    elif not _watched_courses():
        logger.warning("Canvas watcher started with no watched courses; idling")

    # In multi-tenant mode each tick rebuilds the CanvasClient list per
    # platform, so this single legacy instance is only used for the
    # env-token fallback path (single-tenant courses).
    reflow = ReflowClient()

    # Periodic maintenance runs every N watcher ticks so we don't pay the
    # SCAN cost every iteration. Stale-job sweep is fast (a few minutes
    # of staleness is acceptable); retention purge is slow and only needs
    # to run every few hours.
    stale_every = max(1, int(getattr(settings, "canvas_stale_sweep_every_ticks", 5)))
    retention_every = max(1, int(getattr(settings, "canvas_retention_sweep_every_ticks", 240)))
    tick = 0

    while not shutdown_event.is_set():
        try:
            targets = await _enumerate_scan_targets(redis)
        except Exception:
            logger.exception("Failed to enumerate scan targets; skipping tick")
            targets = []

        for course_id, platform in targets:
            try:
                canvas = await _make_canvas_client(redis, platform, course_id)
            except Exception:
                logger.exception(
                    "Cannot build Canvas client for course=%s platform=%s",
                    course_id, getattr(platform, "platform_id", None),
                )
                continue
            try:
                await _scan_course(
                    redis, canvas, reflow, course_id,
                    platform_id=getattr(platform, "platform_id", None),
                )
            except CanvasApiError as exc:
                logger.warning("Canvas scan failed for %s: %s", course_id, exc)
            except Exception:
                logger.exception("Unexpected error scanning course %s", course_id)

        tick += 1

        if tick % stale_every == 0:
            try:
                swept = await sweep_stale_jobs(redis)
                if swept:
                    logger.info("Stale-job sweeper flipped %d job(s) to failed", swept)
            except Exception:
                logger.exception("Stale-job sweep failed")

        if tick % retention_every == 0:
            try:
                summary = await _run_retention_sweep(redis)
                if summary["jobs_removed"] or summary["audit_events_removed"]:
                    logger.info(
                        "Retention sweep removed %d job(s) and %d audit event(s)",
                        summary["jobs_removed"], summary["audit_events_removed"],
                    )
            except Exception:
                logger.exception("Retention sweep failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except TimeoutError:
            continue


async def _run_retention_sweep(redis: Redis) -> dict[str, int]:
    """Translate retention settings (in days) into a purge call.

    Wrapped so the watcher loop stays readable and so tests can patch the
    boundary without monkeypatching settings.
    """
    from ..canvas.state import purge_old_canvas_records

    job_days = int(getattr(settings, "canvas_job_retention_days", 0))
    audit_days = int(getattr(settings, "canvas_audit_retention_days", 0))
    return await purge_old_canvas_records(
        redis,
        job_retention_seconds=job_days * 86400,
        audit_retention_seconds=audit_days * 86400,
    )


async def sweep_stale_jobs(redis: Redis) -> int:
    """Flip any ``processing`` job older than the configured age to ``failed``.

    Why this exists: the bridge worker drives ``processing`` -> ``awaiting_review``
    once Reflow signals completion, but if Reflow's callback is lost or the
    job upload to S3 silently 5xx's, the record sits in ``processing``
    forever and the faculty member sees a spinning dial with no resolution.
    Sweeping ages the record out into ``failed`` after a cap (default
    1 hour) so the modal can show a real error message instead of pretending
    work is in flight.

    Returns the number of jobs the sweep flipped, for logging by the caller.
    """
    import time

    from ..canvas.state import get_job, put_job

    max_age_seconds = int(getattr(settings, "canvas_stale_job_max_age_seconds", 3600))
    cutoff = time.time() - max_age_seconds

    flipped = 0
    cursor = 0
    while True:
        cursor, keys = await redis.scan(
            cursor=cursor, match=tk("canvas:job:*"), count=200
        )
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            job_id = key.rsplit(":", 1)[-1]
            job = await get_job(redis, job_id)
            if job is None or job.status != "processing":
                continue
            if (job.created_at or 0) > cutoff:
                continue
            job.status = "failed"
            job.error = (
                f"Job stuck in 'processing' for more than {max_age_seconds}s; "
                "the bridge worker never received a completion signal. "
                "Re-upload the file to retry."
            )
            await put_job(redis, job)
            logger.warning(
                "Stale job %s (file=%s course=%s) flipped to failed",
                job_id, job.canvas_file_name, job.canvas_course_id,
            )
            flipped += 1
        if cursor == 0:
            break
    return flipped


async def _discover_file_ids(canvas: CanvasClient, course_id: str) -> tuple[dict[str, dict], set[str]]:
    """Discover every file id referenced in any course surface.

    Returns (metadata_by_id, html_referenced_ids). The first contains
    files we already have full metadata for (from listings); the second
    is a set of ids found in HTML that we still need to resolve.
    """

    full: dict[str, dict[str, Any]] = {}
    refs: set[str] = set()

    # 1. Files page
    try:
        for f in await canvas.list_course_pdfs(course_id):
            fid = str(f.get("id") or "")
            if fid:
                full[fid] = f
    except CanvasApiError as exc:
        logger.warning("files listing failed: %s", exc)

    # 2. Folder walk (hidden folders, RCE paperclip uploads)
    try:
        for folder in await canvas.list_course_folders(course_id):
            try:
                for f in await canvas.list_folder_files(str(folder.get("id"))):
                    fid = str(f.get("id") or "")
                    if fid and fid not in full and _is_convertible(f):
                        full[fid] = f
            except CanvasApiError:
                continue
    except CanvasApiError as exc:
        logger.warning("folder walk failed: %s", exc)

    # 3. Modules
    try:
        for module in await canvas.list_modules(course_id):
            try:
                for item in await canvas.list_module_items(course_id, str(module.get("id"))):
                    if (item.get("type") or "").lower() != "file":
                        continue
                    fid = str(item.get("content_id") or "")
                    if fid and fid not in full:
                        refs.add(fid)
            except CanvasApiError:
                continue
    except CanvasApiError as exc:
        logger.warning("module walk failed: %s", exc)

    # 4. Pages — body HTML scanned for file refs
    try:
        pages = await canvas.list_pages(course_id)
        for p in pages:
            url = p.get("url")
            if not url:
                continue
            try:
                page = await canvas.get_page(course_id, url)
            except CanvasApiError:
                continue
            refs |= _extract_file_ids_from_html(page.get("body"))
    except CanvasApiError as exc:
        logger.warning("pages scan failed: %s", exc)

    # 5. Discussions + announcements
    try:
        topics = await canvas.list_discussion_topics(course_id, include_announcements=True)
        for t in topics:
            refs |= _extract_file_ids_from_html(t.get("message"))
            # Attachments directly on the topic
            for att in t.get("attachments") or []:
                aid = str(att.get("id") or "")
                if aid:
                    refs.add(aid)
            try:
                entries = await canvas.list_discussion_entries(course_id, str(t.get("id")))
            except CanvasApiError:
                continue
            for e in entries:
                refs |= _extract_file_ids_from_html(e.get("message"))
                for att in e.get("attachments") or []:
                    aid = str(att.get("id") or "")
                    if aid:
                        refs.add(aid)
    except CanvasApiError as exc:
        logger.warning("discussions scan failed: %s", exc)

    # 6. Assignments
    try:
        for a in await canvas.list_assignments(course_id):
            refs |= _extract_file_ids_from_html(a.get("description"))
    except CanvasApiError as exc:
        logger.warning("assignments scan failed: %s", exc)

    # 7. Quizzes
    try:
        for q in await canvas.list_quizzes(course_id):
            refs |= _extract_file_ids_from_html(q.get("description"))
    except CanvasApiError as exc:
        logger.warning("quizzes scan failed: %s", exc)

    # 8. Syllabus body
    try:
        syl = await canvas.get_course_syllabus(course_id)
        refs |= _extract_file_ids_from_html(syl.get("syllabus_body"))
    except CanvasApiError as exc:
        logger.warning("syllabus scan failed: %s", exc)

    # Drop ids we already have full metadata for
    refs -= set(full.keys())
    return full, refs


async def _resolve_refs(canvas: CanvasClient, refs: set[str]) -> dict[str, dict[str, Any]]:
    """Resolve a set of file ids to PDF metadata. Non-PDFs are dropped."""
    out: dict[str, dict[str, Any]] = {}
    for fid in refs:
        try:
            meta = await canvas.get_file_metadata(fid)
        except CanvasApiError:
            continue
        if _is_pdf(meta):
            out[fid] = meta
    return out


async def _scan_course(
    redis: Redis,
    canvas: CanvasClient,
    reflow: ReflowClient,
    course_id: str,
    *,
    platform_id: str | None = None,
) -> None:
    full, refs = await _discover_file_ids(canvas, course_id)
    resolved = await _resolve_refs(canvas, refs)
    all_pdfs = {**full, **resolved}
    if not all_pdfs:
        logger.debug("no PDFs discovered in course %s", course_id)
        return

    new_count = 0
    for file_id, f in all_pdfs.items():
        if await already_processed(redis, course_id, file_id):
            continue

        filename = f.get("display_name") or f.get("filename") or f"file-{file_id}.pdf"
        # Canvas's Files API only exposes ``user_id`` when the calling
        # token has admin scope; for the user-OAuth path it's typically
        # empty. Falling back to the course owner (the instructor whose
        # OAuth token is authorizing the scan) gives the bridge a real
        # ``canvas_user_id`` to look up the per-user token with, which
        # is the only auth that works against Canvas Cloud /api/v1.
        uploader = str(f.get("user_id") or f.get("uploaded_by") or "")
        if not uploader:
            try:
                owner = await get_course_owner(redis, course_id)
                if owner:
                    uploader = str(owner)
                    logger.debug(
                        "Watcher: file %s/%s has no uploader id; using "
                        "course owner %s as canvas_user_id for routing",
                        course_id, file_id, uploader,
                    )
            except Exception:  # noqa: BLE001 -- defensive
                logger.exception(
                    "Watcher: get_course_owner failed for %s; submitting "
                    "job with empty canvas_user_id (bridge will fall back "
                    "to LTI Advantage service token)",
                    course_id,
                )

        # Check the per-course monthly Claude-API spend cap. ``reserve_submission``
        # atomically charges the pre-flight estimate and refunds it if we
        # would blow the cap. When the cap is unlimited (default 0), this
        # still records the spend - useful for cost reporting later.
        allowed, spend_info = await reserve_submission(redis, course_id)
        if not allowed:
            logger.warning(
                "Skipping submission: course %s would exceed monthly spend cap "
                "(spend=%d cents, cap=%d cents)",
                course_id, spend_info["spend_after_cents"], spend_info["cap_cents"],
            )
            # Mark processed so we don't keep retrying every tick. Operator
            # raising the cap can clear the processed set for that course.
            await mark_processed(redis, course_id, file_id)
            continue

        logger.info(
            "Submitting new document to Reflow: course=%s file=%s name=%s",
            course_id, file_id, filename,
        )
        try:
            content = await canvas.download_file(file_id)
            # submit_document handles any supported format (PDF / DOCX / PPTX);
            # mime type is inferred from filename.
            reflow_job_id = await reflow.submit_document(filename, content)
        except Exception:
            logger.exception("Failed to submit file %s/%s", course_id, file_id)
            continue

        await put_job(
            redis,
            CanvasJob(
                reflow_job_id=reflow_job_id,
                canvas_file_id=file_id,
                canvas_file_name=filename,
                canvas_course_id=course_id,
                canvas_user_id=uploader,
                status="processing",
                created_at=time.time(),
                # Stamp the platform on every job created in multi-tenant
                # mode so the bridge worker can route the result back to
                # the right Canvas with the right credentials. Legacy
                # single-tenant scans pass None here, which the bridge
                # interprets as "use the env-token client".
                platform_id=platform_id,
            ),
        )
        await mark_processed(redis, course_id, file_id)
        new_count += 1

    if new_count:
        logger.info("Course %s scan: %d new PDFs submitted (%d total discovered)",
                    course_id, new_count, len(all_pdfs))
    else:
        logger.debug("Course %s scan: 0 new (%d already known)", course_id, len(all_pdfs))
