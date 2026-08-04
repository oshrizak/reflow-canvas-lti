#!/usr/bin/env python3
"""Find what makes Canvas's edge WAF reject a Page write.

Canvas Cloud sits behind CloudFront + AWS WAF. Some ``PUT
/api/v1/courses/:id/pages/:url`` requests are rejected *before reaching
Canvas*, with an HTML 403 whose body says "Request blocked". A Canvas
permission error is JSON, so the body type alone tells you which layer
answered — but the status code is identical, and reasoning from the code
produced four wrong diagnoses in one afternoon (missing OAuth scope,
deleted-page tombstone, refresh scope erosion, update-vs-create split).
None were real.

Size is not the explanation either. In course 50594 a 115,247-character
body published fine while a 25,043-character one was blocked on every
retry. AWS WAF inspects only the first ~8KB of a request body, so the
trigger is something in the *opening* of the blocked document rather than
its length — which is consistent with the blocked pages being dense with
image markup up front while the passing one opens with prose.

This script replaces argument with measurement. Two modes:

``--synthetic``
    Send controlled payloads — prose only, image tags at varying density,
    long URLs, and so on — and report which shapes are blocked. Isolates
    the rule without needing a real document.

``--job JOB_ID``
    Take a real converted document, confirm it is blocked, then binary
    search for the shortest leading fragment that still gets blocked.
    That fragment names the construct.

Everything is written to a scratch page whose title is passed with
``--page-title`` (default below). Nothing touches a real course page.

Usage
-----
Run inside the connector container, which already has Redis, the platform
records and the stored user token:

    docker exec -it reflow-canvas-lti-connector-1 \\
        python /app/scripts/probe_canvas_waf.py --course 50594 --synthetic

    docker exec -it reflow-canvas-lti-connector-1 \\
        python /app/scripts/probe_canvas_waf.py --course 50594 \\
            --job f8cbffcf-01ef-4dde-a622-dc1d5843edd9

Read-only against your real content; the only writes are to the scratch
page. Delete it when you're done.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from connector.canvas.client import CanvasClient
from connector.canvas.errors import CanvasApiError
from connector.canvas.markdown_to_html import render
from connector.canvas.reflow_client import ReflowClient
from connector.canvas.state import get_job
from connector.config import settings
from connector.lti.platform_store import (
    get_course_owner,
    get_platform,
    get_platform_for_course,
)
from redis.asyncio import Redis

SCRATCH_TITLE = "Reflow WAF probe (safe to delete)"


def _is_waf_block(exc: CanvasApiError) -> bool:
    """WAF answers in HTML; Canvas answers in JSON.

    This is the single most useful discriminator in the whole problem and
    it costs one string check.
    """
    body = (exc.body or "").lstrip().lower()
    return body.startswith("<!doctype html") or "request could not be satisfied" in body


def _verdict(exc: CanvasApiError | None) -> str:
    if exc is None:
        return "PASS"
    if _is_waf_block(exc):
        return "BLOCKED (WAF)"
    return f"REJECTED (Canvas {exc.status_code})"


async def _attempt(canvas: CanvasClient, course: str, slug: str, body: str) -> str:
    try:
        await canvas.update_page(course, slug, SCRATCH_TITLE, body)
        return _verdict(None)
    except CanvasApiError as exc:
        return _verdict(exc)


# --- synthetic fixtures ------------------------------------------------
# Each returns (label, html). Ordered cheapest-signal-first: if plain prose
# of the same length passes and image-dense markup fails, the rule is about
# the markup, and the remaining fixtures narrow which part of it.

_PROSE = (
    "<p>Students will describe typical characteristics of an enzyme active "
    "site and apply the term motif to enzyme active sites.</p>\n"
)


def _img(n: int, course: str) -> str:
    return (
        f'<p><img src="https://csueb.instructure.com/courses/{course}'
        f'/files/{7000000 + n}/preview" alt="Figure {n}"></p>\n'
    )


def _synthetic_fixtures(course: str) -> list[tuple[str, str]]:
    prose_12k = _PROSE * 60
    return [
        ("prose only, ~1KB", _PROSE * 5),
        ("prose only, ~12KB", prose_12k),
        ("1 image + prose", _img(1, course) + prose_12k),
        ("5 images then prose", "".join(_img(i, course) for i in range(5)) + prose_12k),
        ("12 images then prose", "".join(_img(i, course) for i in range(12)) + prose_12k),
        (
            "12 images, no alt",
            "".join(
                _img(i, course).replace(f' alt="Figure {i}"', ' alt=""')
                for i in range(12)
            ) + prose_12k,
        ),
        (
            "12 images, relative src",
            "".join(
                f'<p><img src="figures/figure-{i}.png" alt="Figure {i}"></p>\n'
                for i in range(12)
            ) + prose_12k,
        ),
        ("image-only, 12 tags", "".join(_img(i, course) for i in range(12))),
    ]


async def _run_synthetic(canvas: CanvasClient, course: str, slug: str) -> None:
    print("\nSynthetic probes — same target page, different body shapes\n")
    print(f"{'payload':<28} {'bytes':>8}  result")
    print("-" * 60)
    for label, body in _synthetic_fixtures(course):
        result = await _attempt(canvas, course, slug, body)
        print(f"{label:<28} {len(body.encode()):>8}  {result}")
        await asyncio.sleep(0.4)  # be kind to the edge; avoid rate limiting
    print(
        "\nRead it this way: if prose passes at a size where image-dense "
        "markup fails, length is not the trigger and the markup is.\n"
    )


# --- real-document bisect ----------------------------------------------


async def _run_bisect(
    canvas: CanvasClient, redis: Redis, course: str, slug: str, job_id: str
) -> None:
    job = await get_job(redis, job_id)
    if job is None:
        sys.exit(f"No canvas job record for {job_id}")

    # Reuse the bridge's own client rather than hand-rolling the Reflow
    # call, so the markdown here is byte-identical to what gets published.
    reflow = ReflowClient()
    status = await reflow.get_status(job_id)
    md_url = status.get("markdown_url") or status.get("result_url")
    if not md_url:
        sys.exit(f"Reflow has no markdown for {job_id} (status={status.get('status')})")
    markdown = await reflow.fetch_markdown(md_url)

    rendered = render(markdown, title=job.canvas_file_name)
    body = rendered.html

    print(f"\nDocument: {job.canvas_file_name}")
    print(f"Rendered body: {len(body.encode())} bytes\n")

    whole = await _attempt(canvas, course, slug, body)
    print(f"whole body -> {whole}")
    if whole == "PASS":
        print(
            "\nThe full body passed, so there is nothing to bisect. Either the "
            "rule changed or this document was never the blocked one.\n"
        )
        return

    # Shortest blocking prefix. WAF only inspects the opening of the body,
    # so the answer is expected to land well under 8KB.
    lo, hi = 0, len(body)
    while lo + 200 < hi:
        mid = (lo + hi) // 2
        result = await _attempt(canvas, course, slug, body[:mid])
        print(f"  first {mid:>7} bytes -> {result}")
        if result == "PASS":
            lo = mid
        else:
            hi = mid
        await asyncio.sleep(0.4)

    print(f"\nShortest blocking prefix: ~{hi} bytes. The construct is in:\n")
    print(body[max(0, hi - 600):hi])
    print("\nShow that fragment to whoever runs the WAF; it is the evidence.\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="Canvas course id")
    ap.add_argument("--job", default="", help="Reflow job id to bisect")
    ap.add_argument("--synthetic", action="store_true", help="run shape probes")
    ap.add_argument("--page-slug", default="reflow-waf-probe-safe-to-delete")
    args = ap.parse_args()

    if not args.job and not args.synthetic:
        ap.error("choose --synthetic, --job JOB_ID, or both")

    redis: Redis = Redis.from_url(settings.redis_url)
    # get_platform_for_course returns an id; the client wants the record.
    platform_id = await get_platform_for_course(redis, args.course)
    if not platform_id:
        sys.exit(f"No LTI platform recorded for course {args.course}")
    platform = await get_platform(redis, platform_id)
    if platform is None:
        sys.exit(f"Platform {platform_id} has no stored record")
    owner = await get_course_owner(redis, args.course)
    if not owner:
        sys.exit("No course owner; launch the tool once so a token is stored")
    canvas = CanvasClient.from_user_token(redis, platform, owner)

    # Create the scratch page first so every probe is an update, holding
    # the create/update distinction constant across runs.
    try:
        await canvas.create_page(
            args.course, SCRATCH_TITLE, "<p>probe</p>", published=False
        )
        print(f"Created scratch page '{SCRATCH_TITLE}'")
    except CanvasApiError as exc:
        print(f"Scratch page not created ({exc.status_code}); assuming it exists")

    if args.synthetic:
        await _run_synthetic(canvas, args.course, args.page_slug)
    if args.job:
        await _run_bisect(canvas, redis, args.course, args.page_slug, args.job)

    await redis.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
