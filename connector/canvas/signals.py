"""Derive real per-document accessibility signals from pipeline output.

The legacy ``score_from_reflow_result`` expected Reflow to surface a
structured ``signals`` field in its status payload. In practice the
upstream Reflow API doesn't populate this — Reflow is a
PDF->markdown converter, not an accessibility analyzer. That left
``score_from_reflow_result({})`` returning a deceptive flat 15/red
on every document because the "no tables == all tables semantic"
branch awarded the table weight by default.

This module fixes that by *deriving* signals from artifacts we
already have on hand at conversion time:

  * The pipeline's markdown output (heading structure, image+alt
    presence, table-vs-image-of-table, math, code).
  * The PDF classifier verdict (``standard`` vs ``scanned``).
  * Source language detection from the markdown body.

The result is a dict shaped exactly the way
``score_from_reflow_result`` expects, so the score function can
remain unchanged and we get genuine, per-file variance — not a
constant fallback.

Anti-claim: these signals are a *conversion-quality* heuristic, NOT
a WCAG conformance proof. They're a useful signal for routing
documents to human review, not a substitute for it.
"""

from __future__ import annotations

import re
from typing import Any

# Heading patterns. ATX style (# Heading) is what docling outputs.
_HEADING_RE = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)

# Markdown image syntax: ![alt](src). Empty alt is the "decorative" or
# "missing alt" case — we count those separately.
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# A pipe-style table requires both a header row and a separator row
# (|---|---|). Pipe rows that aren't followed by a separator are just
# inline text. This conservative match catches genuine tables.
_TABLE_BLOCK_RE = re.compile(
    r"(^\|[^\n]+\|\s*\n\|[\s\-:|]+\|\s*\n(?:\|[^\n]*\|\s*\n?)+)",
    re.MULTILINE,
)

# Fenced code blocks.
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)

# Display math (LaTeX-ish). Conservative — matches $$...$$ and \[...\].
_MATH_DISPLAY_RE = re.compile(r"(\$\$[\s\S]*?\$\$)|(\\\[[\s\S]*?\\\])")


def _detect_language(text: str) -> str | None:
    """Best-effort language code from common cues.

    We deliberately don't pull in a heavy detector — most academic
    documents are English, and when they're not, this function is
    intentionally conservative. Returns ``None`` when we can't make
    a confident call (which is honest; the WCAG rule cares whether
    a language is *declared*, and that's the publisher's job).
    """
    if not text or not text.strip():
        return None
    # If the sample is overwhelmingly ASCII letters, call it English.
    # This is a heuristic — the real fix is asking the user to confirm
    # the language at publish time, not guessing here.
    sample = text[:4000]
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return None
    ascii_ratio = sum(1 for c in letters if ord(c) < 128) / len(letters)
    if ascii_ratio > 0.95:
        return "en"
    return None  # let the publisher set lang explicitly


def derive_signals_from_markdown(
    markdown: str,
    *,
    pdf_classification: str | None = None,
    ocr_was_run: bool = False,
) -> dict[str, Any]:
    """Build a Reflow-style signals dict from converted markdown.

    Parameters
    ----------
    markdown : the pipeline's final markdown output (post-cleanup).
    pdf_classification : verdict from ``PdfClassifier`` if available
        ("standard", "scanned", etc.).
    ocr_was_run : True when the pipeline ran OCR on a scanned source.
        Even if OCR succeeded, the resulting text layer is synthetic
        and deserves a lower confidence floor than a born-digital PDF.

    Returns
    -------
    A dict with the exact keys ``score_from_reflow_result`` looks for:
    ``has_text_layer``, ``heading_levels``, ``images_total``,
    ``images_with_alt``, ``tables_total``, ``tables_semantic``,
    ``reading_order_linear``, ``language``.
    """
    md = markdown or ""

    # has_text_layer: True if the source PDF was born-digital. For OCR'd
    # scans the text layer exists but is synthetic; mark it as present
    # (selectable text DOES exist now) but note that ocr_was_run is
    # important downstream for the "low-confidence scan" review flag.
    has_text_layer = bool(md.strip()) and pdf_classification != "no_text"

    # Headings: collect distinct heading levels actually used.
    levels = sorted({len(m.group(1)) for m in _HEADING_RE.finditer(md)})

    # Images: count all image references; an image is "with alt" when the
    # alt text inside ``![ ... ]`` is non-empty and not a placeholder.
    images = list(_IMAGE_RE.finditer(md))
    images_total = len(images)
    images_with_alt = sum(
        1 for m in images
        if (m.group(1) or "").strip()
        and (m.group(1) or "").strip().lower() not in {"image", "figure", "fig", "img"}
    )

    # Tables: each match is one full table block (header + separator +
    # body rows). Pipe-style tables ARE semantic markup once they
    # render, so we count them as semantic. Image-of-table cases
    # appear as just an image, so they're counted under images
    # without alt and don't show up in tables_total -- exactly the
    # right signal.
    tables_total = len(_TABLE_BLOCK_RE.findall(md))
    tables_semantic = tables_total  # every pipe-table is semantic by def

    # Reading order: with docling output, the markdown ordering reflects
    # the visual top-to-bottom reading order docling extracted. We treat
    # this as ``True`` for the born-digital path. For scans (OCR'd) the
    # ordering can be wonky on multi-column pages, so flag it as False
    # there to push the document into human review.
    reading_order_linear = pdf_classification != "scanned" and not ocr_was_run

    language = _detect_language(md)

    return {
        "has_text_layer": has_text_layer,
        "heading_levels": levels,
        "images_total": images_total,
        "images_with_alt": images_with_alt,
        "tables_total": tables_total,
        "tables_semantic": tables_semantic,
        "reading_order_linear": reading_order_linear,
        "language": language,
        # Provenance — handy for diagnostics, ignored by score function.
        "_source": "derived",
        "_pdf_classification": pdf_classification,
        "_ocr_was_run": ocr_was_run,
        "_code_blocks": len(_CODE_FENCE_RE.findall(md)),
        "_math_display": len(_MATH_DISPLAY_RE.findall(md)),
    }
