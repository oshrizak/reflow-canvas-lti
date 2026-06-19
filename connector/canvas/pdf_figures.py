"""Extract figure images directly from the source PDF.

Why this module exists: Reflow's figure-extraction pipeline emits PNG
crops with a vision-model tile/segmentation grid baked into the
output. Embedding those bytes makes the rendered review surface and
the published Canvas Page look visibly different from the original
document. The PDF's own embedded rasters carry the same imagery
without the overlay, so we pull them from there instead. Reflow
still owns the markdown and the alt text — only the image bytes
change source.

Matching strategy: Reflow tells us each figure's PAGE (1-indexed) but
not its bbox. For single-figure pages, page → image is unambiguous.
For multi-figure pages we group Reflow figures by page, sort by
``figure_id`` (Reflow assigns them in reading order), and the k-th
Reflow figure on page P maps to the k-th sub-region embedded raster
on page P sorted top-to-bottom, then left-to-right.

This works for typical academic layouts. It will misfire when:

  * The figure is rendered as PDF vector ops (charts, line drawings)
    — there is no embedded raster to extract. The caller catches
    ``PdfFigureNotFoundError`` and falls back to the upstream Reflow copy.
  * A page has more figure-shaped rasters than Reflow reports (e.g.,
    decorative icons that Reflow filtered out). The reading-order
    sort then includes the decorations and the index lands on the
    wrong image. The sub-region filter below drops obvious full-page
    layers (scanned-PDF backgrounds) but cannot tell decoration from
    figure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


class PdfFigureNotFoundError(Exception):
    """No PDF raster could be matched for the requested Reflow figure id."""


@dataclass(frozen=True)
class ExtractedFigure:
    """Bytes + content type for one image lifted from the source PDF."""

    image_bytes: bytes
    content_type: str  # ``image/png``, ``image/jpeg``, etc.


# Image formats browsers render directly. Anything else gets normalized
# to PNG via PyMuPDF's Pixmap so the iframe doesn't end up with a broken
# image icon for JPEG-2000 / TIFF / BMP sources.
_WEB_NATIVE_EXTS = {"png", "jpg", "jpeg", "gif"}

_EXT_TO_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
}


def extract_figure_for_reflow_id(
    pdf_bytes: bytes,
    reflow_figures: list[dict[str, Any]],
    requested_figure_id: str,
) -> ExtractedFigure:
    """Return the PDF embedded raster matching ``requested_figure_id``.

    Args:
        pdf_bytes: The original PDF source as bytes.
        reflow_figures: The ``status['figures']`` list from Reflow's
            ``/api/v1/documents/{id}`` response. Each entry needs at
            minimum ``figure_id`` and ``page``.
        requested_figure_id: The Reflow id (e.g., ``"figure-3"``) the
            caller wants.

    Raises:
        PdfFigureNotFoundError: The figure isn't in Reflow's list, its page
            metadata is missing, the page doesn't exist in the PDF, or
            no extractable raster was found at the expected reading-
            order position on that page.
    """
    target = next(
        (
            f for f in reflow_figures
            if str(f.get("figure_id") or "") == requested_figure_id
        ),
        None,
    )
    if target is None:
        raise PdfFigureNotFoundError(
            f"figure_id {requested_figure_id!r} not present in Reflow status"
        )
    page_num = int(target.get("page") or 0)
    if page_num < 1:
        raise PdfFigureNotFoundError(
            f"figure {requested_figure_id!r} has no page metadata"
        )

    # Reading-order rank among all Reflow figures on the same page.
    same_page = sorted(
        [f for f in reflow_figures if int(f.get("page") or 0) == page_num],
        key=lambda f: _figure_id_sort_key(str(f.get("figure_id") or "")),
    )
    try:
        index_on_page = next(
            i for i, f in enumerate(same_page)
            if str(f.get("figure_id") or "") == requested_figure_id
        )
    except StopIteration:  # pragma: no cover — by construction unreachable
        raise PdfFigureNotFoundError(
            f"{requested_figure_id!r} dropped out of its own page bucket"
        ) from None

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page_num > doc.page_count:
            raise PdfFigureNotFoundError(
                f"PDF has {doc.page_count} pages; Reflow expected page {page_num}"
            )
        page = doc[page_num - 1]
        page_w = page.rect.width
        page_h = page.rect.height

        # ``get_image_info(xrefs=True)`` returns one entry per image USE
        # on the page (an image used twice yields two entries) with the
        # placement bbox and the ``xref`` we can extract from.
        all_imgs = page.get_image_info(xrefs=True)

        # Drop full-page background layers. Scanned PDFs commonly have
        # the page itself as a giant image covering the whole page area;
        # that's never the figure the user means. Use an area-based
        # threshold (95% of the page) so we catch both
        # ``bbox == (0, 0, page_w, page_h)`` and the centered-with-margins
        # case (PyMuPDF preserves the source raster's aspect ratio when
        # the placement rect doesn't match exactly, leaving a few-pixel
        # margin that a strict bbox check would miss).
        page_area = page_w * page_h
        full_page_threshold = 0.95
        sub_region = [
            info for info in all_imgs
            if (info["bbox"][2] - info["bbox"][0])
               * (info["bbox"][3] - info["bbox"][1])
               < full_page_threshold * page_area
        ]
        # Reading order: top-to-bottom (y0), then left-to-right (x0).
        sub_region.sort(key=lambda info: (info["bbox"][1], info["bbox"][0]))

        if index_on_page >= len(sub_region):
            raise PdfFigureNotFoundError(
                f"page {page_num} has {len(sub_region)} extractable raster(s) "
                f"but Reflow expected at least {index_on_page + 1} "
                f"(for {requested_figure_id!r})"
            )
        chosen_xref = sub_region[index_on_page]["xref"]
        img = doc.extract_image(chosen_xref)
        ext = str(img.get("ext") or "png").lower()
        if ext in _WEB_NATIVE_EXTS:
            return ExtractedFigure(
                image_bytes=img["image"],
                content_type=_EXT_TO_CONTENT_TYPE[ext],
            )
        # JPEG-2000 / TIFF / BMP / etc. → normalize to PNG so the
        # downstream consumer (browser, Canvas Files) renders it.
        pix = fitz.Pixmap(img["image"])
        try:
            return ExtractedFigure(
                image_bytes=pix.tobytes("png"),
                content_type="image/png",
            )
        finally:
            pix = None  # noqa: F841 — release C-level buffer eagerly
    finally:
        doc.close()


def _figure_id_sort_key(figure_id: str) -> tuple[int, str]:
    """Sort ``figure-2`` before ``figure-10`` and ``figure-7a`` after ``figure-7``.

    Reflow's ids look like ``figure-N`` or ``figure-Na`` (the trailing
    letter denotes sub-parts of a multi-panel figure). Splitting the
    numeric prefix from the letter suffix gives a natural reading order.
    """
    tail = figure_id.removeprefix("figure-")
    digits: list[str] = []
    rest = ""
    for i, ch in enumerate(tail):
        if ch.isdigit():
            digits.append(ch)
        else:
            rest = tail[i:]
            break
    return (int("".join(digits) or "0"), rest)
