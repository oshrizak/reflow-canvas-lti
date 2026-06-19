"""Re-upload figures for existing jobs using PDF-extracted bytes.

Why: the bridge worker now pulls figure bytes from the source PDF's
embedded rasters instead of Reflow's S3 PNGs (which carry a vision-
pipeline tile grid baked in). Jobs created before that change still
have the gridded copies in their Canvas Files folder, so this script
overwrites them in place and rewrites the published Canvas Page's
``<img>`` tags to the refreshed file URLs.

Usage (inside the connector container)::

    python -m connector.tools.reprocess_figures --course-id 50594
    python -m connector.tools.reprocess_figures --job-id <uuid>

Idempotent: re-running the script after a successful pass is a no-op
beyond the Canvas overwrite (the file URLs may rotate but the page is
re-PUT to match).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from ..canvas.client import CanvasClient
from ..canvas.pdf_figures import (
    PdfFigureNotFound,
    extract_figure_for_reflow_id,
)
from ..canvas.reflow_client import ReflowClient, rewrite_presigned_url
from ..canvas.state import CanvasJob, get_job, put_job
from ..canvas.tenant import tk
from ..dependencies import get_redis_client
from ..workers.reflow_bridge_worker import (
    _FIGURE_FOLDER,
    _canvas_client_for_job,
)

logger = logging.getLogger(__name__)


async def _reprocess_one(redis, job: CanvasJob) -> dict[str, int]:
    """Re-upload every figure for one job from PDF-extracted bytes.

    Returns a small dict of counters for caller logging.
    """
    counters = {"uploaded": 0, "skipped_vector": 0, "fell_back_to_s3": 0, "failed": 0}

    canvas = await _canvas_client_for_job(redis, job)
    pdf_bytes = await canvas.download_file(job.canvas_file_id)

    reflow = ReflowClient()
    status = await reflow.get_status(job.reflow_job_id)
    figures = status.get("figures") or status.get("stored_figures") or []

    new_canvas_urls: dict[str, str] = {}
    for fig in figures:
        fid = str(fig.get("figure_id") or "").strip()
        src = str(fig.get("url") or "").strip()
        if not fid:
            continue
        ref = f"figures/{fid}.png"
        canvas_fig_name = f"{job.canvas_file_id}-{fid}.png"

        figure_bytes: bytes | None = None
        content_type = "image/png"
        try:
            extracted = extract_figure_for_reflow_id(pdf_bytes, figures, fid)
            figure_bytes = extracted.image_bytes
            content_type = extracted.content_type
        except PdfFigureNotFound as exc:
            logger.info("Job %s fig %s: PDF extraction skipped: %s",
                        job.reflow_job_id, fid, exc)
            counters["skipped_vector"] += 1

        # Fall back to Reflow's S3 copy when PDF extraction couldn't match
        # (e.g., vector figure). Better to keep the gridded copy than lose
        # the figure altogether.
        if figure_bytes is None and src:
            try:
                async with httpx.AsyncClient(timeout=60.0) as hc:
                    r = await hc.get(rewrite_presigned_url(src), follow_redirects=True)
                r.raise_for_status()
                figure_bytes = r.content
                counters["fell_back_to_s3"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Job %s fig %s: S3 fallback fetch failed",
                    job.reflow_job_id, fid,
                )
                counters["failed"] += 1
                continue

        if figure_bytes is None:
            counters["failed"] += 1
            continue

        try:
            uploaded = await canvas.upload_course_file(
                job.canvas_course_id,
                canvas_fig_name,
                figure_bytes,
                content_type=content_type,
                folder_path=_FIGURE_FOLDER,
            )
            canvas_url = str(uploaded.get("url") or "")
            if canvas_url:
                new_canvas_urls[ref] = canvas_url
                counters["uploaded"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "Job %s fig %s: Canvas upload failed", job.reflow_job_id, fid,
            )
            counters["failed"] += 1

    if not new_canvas_urls:
        logger.warning("Job %s: no figures re-uploaded", job.reflow_job_id)
        return counters

    # Rewrite the published Canvas Page so its <img src=…> tags pick up
    # the new file URLs. Canvas rotates the ``verifier`` query param on
    # overwrite, so the page's existing <img> tags would 401 without
    # this. When there's no Canvas Page yet (page_failed state), skip
    # the rewrite — the alt-format proxy renders from figure_canvas_urls
    # directly.
    old_canvas_urls = dict(job.figure_canvas_urls or {})
    job.figure_canvas_urls = new_canvas_urls

    if job.canvas_page_url and old_canvas_urls:
        try:
            page = await canvas.get_page(job.canvas_course_id, job.canvas_page_url)
            body = str(page.get("body") or "")
            title = str(page.get("title") or job.canvas_file_name)
            for ref, new_url in new_canvas_urls.items():
                prev = old_canvas_urls.get(ref)
                if prev and prev != new_url:
                    body = body.replace(prev, new_url)
            await canvas.update_page(
                job.canvas_course_id, job.canvas_page_url,
                title=title, body_html=body,
            )
            logger.info("Job %s: rewrote Canvas Page with refreshed figure URLs",
                        job.reflow_job_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Job %s: Canvas Page rewrite failed (figure files were re-uploaded "
                "but the page still points at the old URLs)", job.reflow_job_id,
            )

    await put_job(redis, job)
    return counters


async def _list_jobs_for_course(redis, course_id: str) -> list[CanvasJob]:
    """Every job we have stored for a course, regardless of status.

    The ``pending`` set only carries ``awaiting_review`` jobs; this
    scans the full ``canvas:job:*`` keyspace to also include
    ``published``, ``page_failed``, etc. (which is what the user wants
    when backfilling clean figure bytes).
    """
    out: list[CanvasJob] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(
            cursor=cursor, match=tk("canvas:job:*"), count=200,
        )
        for raw in keys:
            key = raw.decode() if isinstance(raw, bytes) else raw
            job_id = key.rsplit(":", 1)[-1]
            job = await get_job(redis, job_id)
            if job is not None and job.canvas_course_id == course_id:
                out.append(job)
        if cursor == 0:
            break
    return out


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    redis = await anext(get_redis_client())

    if args.job_id:
        job = await get_job(redis, args.job_id)
        if job is None:
            print(f"job not found: {args.job_id}", file=sys.stderr)
            return 1
        jobs = [job]
    else:
        jobs = await _list_jobs_for_course(redis, args.course_id)
        if not jobs:
            print(f"no jobs found for course {args.course_id}", file=sys.stderr)
            return 1

    print(f"reprocessing {len(jobs)} job(s)…")
    total = {"uploaded": 0, "skipped_vector": 0, "fell_back_to_s3": 0, "failed": 0}
    for job in jobs:
        print(f"  [{job.reflow_job_id}] {job.canvas_file_name}")
        try:
            stats = await _reprocess_one(redis, job)
            for k, v in stats.items():
                total[k] += v
            print(f"    uploaded={stats['uploaded']} "
                  f"skipped_vector={stats['skipped_vector']} "
                  f"fell_back_to_s3={stats['fell_back_to_s3']} "
                  f"failed={stats['failed']}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("reprocess failed for job %s", job.reflow_job_id)
            print(f"    ERROR: {exc}")
            total["failed"] += 1

    print(f"done. totals: {total}")
    return 0 if total["failed"] == 0 else 2


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--course-id", help="Process every job in this Canvas course.")
    g.add_argument("--job-id", help="Process a single Reflow job id.")
    args = p.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
