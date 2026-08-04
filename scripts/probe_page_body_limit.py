#!/usr/bin/env python3
"""Measure the real maximum Canvas Page body size for your institution.

Why this exists
---------------
``reflow_bridge_worker.py`` publishes a small *stub* page that links out to
tool-hosted HTML, because a Canvas Cloud edge WAF was observed rejecting Page
REST writes with a CloudFront "The request could not be satisfied" 403 once
the body exceeded roughly 8 KB. That number came from a single incident and
was never pinned down.

It matters a lot. If the true ceiling is ~8 KB, a 13-page document (~25-35 KB
of semantic HTML) can never live inline and the only fully-native option is
splitting it across several Pages. If the ceiling is really 200 KB, the stub
is unnecessary and the whole document can go straight into one Canvas Page —
no new tab, no hosted viewer.

This script binary-searches the limit against a real course using a scratch
page, then deletes it.

Usage
-----
    export CANVAS_BASE_URL="https://csueb.instructure.com"
    export CANVAS_TOKEN="<a token with manage_wiki on the course>"
    export CANVAS_COURSE_ID="50594"

    python scripts/probe_page_body_limit.py

Add ``--keep`` to leave the scratch page behind for inspection.

Notes
-----
* Uses a manually generated Canvas API token, NOT the connector's OAuth
  tokens. Generate one at /profile/settings -> "+ New Access Token" and
  revoke it when you're done.
* The scratch page is created unpublished and deleted at the end, so
  students never see it.
* Body filler is inert ``<p>`` text, which is what Canvas's sanitiser is
  most permissive about. A real document body with figures and headings may
  behave slightly differently, so treat the result as an upper bound and
  leave headroom.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

SCRATCH_TITLE = "Reflow WAF probe (safe to delete)"

# Search window. 512 KB upper bound is far past any plausible limit.
LOW_BYTES = 1_024
HIGH_BYTES = 512 * 1_024
TOLERANCE = 512  # stop when the window is this tight


def _body_of_size(target: int) -> str:
    """Return an HTML body of approximately ``target`` bytes."""
    head = "<p>Reflow page-body probe. This page is safe to delete.</p>\n"
    filler_unit = "<p>" + ("x" * 96) + "</p>\n"
    remaining = max(0, target - len(head))
    return head + filler_unit * (remaining // len(filler_unit))


def _classify(resp: httpx.Response) -> str:
    """Return 'ok', 'waf', or 'other'."""
    if resp.status_code < 400:
        return "ok"
    text = (resp.text or "")[:400].lower()
    if resp.status_code == 403 and (
        "request could not be satisfied" in text or "cloudfront" in text
    ):
        return "waf"
    if resp.status_code in (413, 502, 504):
        return "waf"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="don't delete the scratch page")
    args = ap.parse_args()

    base = (os.environ.get("CANVAS_BASE_URL") or "").rstrip("/")
    token = os.environ.get("CANVAS_TOKEN") or ""
    course = os.environ.get("CANVAS_COURSE_ID") or ""
    if not (base and token and course):
        print(
            "Set CANVAS_BASE_URL, CANVAS_TOKEN and CANVAS_COURSE_ID.",
            file=sys.stderr,
        )
        return 2

    headers = {"Authorization": f"Bearer {token}"}
    pages_url = f"{base}/api/v1/courses/{course}/pages"

    with httpx.Client(timeout=60.0, headers=headers) as client:
        # Create the scratch page small, so we have a stable slug to PUT to.
        created = client.post(
            pages_url,
            json={
                "wiki_page": {
                    "title": SCRATCH_TITLE,
                    "body": "<p>probe</p>",
                    "published": False,
                }
            },
        )
        if created.status_code >= 400:
            print(
                f"Could not create the scratch page: HTTP {created.status_code}\n"
                f"{created.text[:400]}",
                file=sys.stderr,
            )
            return 1
        slug = created.json().get("url", "")
        page_url = f"{pages_url}/{slug}"
        print(f"scratch page: {slug}\n")

        def attempt(size: int) -> str:
            resp = client.put(
                page_url, json={"wiki_page": {"body": _body_of_size(size)}}
            )
            verdict = _classify(resp)
            label = {"ok": "OK  ", "waf": "WAF ", "other": "ERR "}[verdict]
            print(f"  {label} {size:>7,} bytes -> HTTP {resp.status_code}")
            if verdict == "other":
                print(f"        {resp.text[:200]}")
            return verdict

        try:
            print("probing:")
            if attempt(LOW_BYTES) != "ok":
                print(
                    "\nEven a 1 KB body failed. That's not a size limit — check the "
                    "token's manage_wiki permission and the course id.",
                )
                return 1

            if attempt(HIGH_BYTES) == "ok":
                print(
                    f"\nRESULT: no limit found below {HIGH_BYTES:,} bytes.\n"
                    "The stub-page workaround is unnecessary here — the full "
                    "document can be written straight into the Canvas Page.",
                )
                return 0

            lo, hi = LOW_BYTES, HIGH_BYTES
            while hi - lo > TOLERANCE:
                mid = (lo + hi) // 2
                if attempt(mid) == "ok":
                    lo = mid
                else:
                    hi = mid

            print(f"\nRESULT: largest accepted body is ~{lo:,} bytes ({lo / 1024:.1f} KB).")
            if lo < 16 * 1024:
                print(
                    "That's below a typical converted document (25-35 KB), so inline\n"
                    "publishing needs the document split across several Pages.",
                )
            else:
                print(
                    "That's roomy enough for most converted documents. Consider\n"
                    "writing the body inline and keeping the stub only as a fallback\n"
                    "for documents that exceed it.",
                )
            return 0
        finally:
            if args.keep:
                print(f"\nleaving scratch page in place: {base}/courses/{course}/pages/{slug}")
            else:
                client.delete(page_url)
                print("\nscratch page deleted.")


if __name__ == "__main__":
    raise SystemExit(main())
