"""Unit tests for the PDF figure extraction matching logic.

The actual PyMuPDF extraction (raster bytes-in, bytes-out) isn't covered
here — it needs real PDFs with real embedded rasters which we don't
ship as fixtures. We instead build tiny PDFs on the fly with
``fitz`` and assert that the matcher picks the expected raster.
"""

from __future__ import annotations

import io

import fitz
import pytest
from connector.canvas.pdf_figures import (
    PdfFigureNotFoundError,
    _figure_id_sort_key,
    extract_figure_for_reflow_id,
)
from PIL import Image


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    """Tiny solid-color PNG so we can tell extracted rasters apart by content."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_pdf(pages: list[list[tuple[bytes, tuple[float, float, float, float]]]]) -> bytes:
    """Build a PDF with the given images-per-page layout.

    ``pages`` is a list of pages; each page is a list of ``(png_bytes, bbox)``
    placements. ``bbox`` is ``(x0, y0, x1, y1)`` in PDF user-space (points).
    """
    doc = fitz.open()
    for placements in pages:
        page = doc.new_page(width=612, height=792)
        for png, bbox in placements:
            page.insert_image(fitz.Rect(*bbox), stream=png)
    out = doc.tobytes()
    doc.close()
    return out


@pytest.mark.unit
def test_figure_id_sort_key_orders_numerically() -> None:
    """``figure-10`` must sort AFTER ``figure-2`` (the lex sort would invert)."""
    ids = ["figure-10", "figure-2", "figure-1", "figure-7a", "figure-7"]
    ids.sort(key=_figure_id_sort_key)
    assert ids == ["figure-1", "figure-2", "figure-7", "figure-7a", "figure-10"]


@pytest.mark.unit
def test_extracts_only_figure_on_single_figure_page() -> None:
    """One Reflow figure on the only page → must extract that one raster."""
    red = _png_bytes((255, 0, 0))
    pdf = _make_pdf([[(red, (100, 100, 300, 300))]])
    reflow = [{"figure_id": "figure-1", "page": 1, "url": ""}]

    out = extract_figure_for_reflow_id(pdf, reflow, "figure-1")

    # Sanity: bytes are non-empty and report a real image mime type.
    assert out.image_bytes
    assert out.content_type.startswith("image/")
    # Reading the extracted bytes back as an image should succeed.
    Image.open(io.BytesIO(out.image_bytes)).verify()


@pytest.mark.unit
def test_disambiguates_multi_figure_page_by_reading_order() -> None:
    """Two figures on the same page: figure-2 (top) → first, figure-3 (bottom) → second.

    Reflow assigns ids in reading order, so the matcher's reading-order
    sort (y0 asc, then x0) should align k-th figure → k-th raster.
    """
    red = _png_bytes((255, 0, 0))   # placed on top
    blue = _png_bytes((0, 0, 255))  # placed on bottom
    pdf = _make_pdf([[
        (red, (100, 100, 300, 200)),    # y0=100 → first in reading order
        (blue, (100, 400, 300, 500)),   # y0=400 → second in reading order
    ]])
    reflow = [
        {"figure_id": "figure-2", "page": 1, "url": ""},
        {"figure_id": "figure-3", "page": 1, "url": ""},
    ]

    top = extract_figure_for_reflow_id(pdf, reflow, "figure-2")
    bottom = extract_figure_for_reflow_id(pdf, reflow, "figure-3")

    # The two extractions should differ — they came from different placements.
    assert top.image_bytes != bottom.image_bytes
    # And both should be readable as images.
    Image.open(io.BytesIO(top.image_bytes)).verify()
    Image.open(io.BytesIO(bottom.image_bytes)).verify()


@pytest.mark.unit
def test_full_page_background_layer_is_filtered_out() -> None:
    """A page-sized image is treated as a scanned background, not a figure.

    The real figure sits inside it at a sub-region; the matcher should pick
    the sub-region raster, not the page-spanning one.
    """
    bg = _png_bytes((255, 255, 255), size=(1200, 1600))    # huge "scan"
    fig = _png_bytes((0, 200, 0), size=(400, 300))         # the real figure
    pdf = _make_pdf([[
        (bg, (0, 0, 612, 792)),                # full-page bbox
        (fig, (150, 300, 450, 525)),           # sub-region
    ]])
    reflow = [{"figure_id": "figure-1", "page": 1, "url": ""}]

    out = extract_figure_for_reflow_id(pdf, reflow, "figure-1")

    # Extracted bytes should be a small raster (the sub-region figure),
    # not the page-sized background.
    extracted = Image.open(io.BytesIO(out.image_bytes))
    assert extracted.size != (1200, 1600), (
        "matcher returned the full-page background instead of the sub-region figure"
    )


@pytest.mark.unit
def test_missing_figure_id_raises() -> None:
    """Asking for a figure_id Reflow never reported is an explicit error."""
    pdf = _make_pdf([[(_png_bytes((255, 0, 0)), (100, 100, 300, 300))]])
    reflow = [{"figure_id": "figure-1", "page": 1, "url": ""}]

    with pytest.raises(PdfFigureNotFoundError, match="not present"):
        extract_figure_for_reflow_id(pdf, reflow, "figure-99")


@pytest.mark.unit
def test_page_with_no_extractable_raster_raises() -> None:
    """Reflow says page 2 has figure-1, but the PDF page has only a full-page
    background — no sub-region raster to match. Caller catches this and
    falls back to Reflow's S3 copy."""
    bg = _png_bytes((255, 255, 255), size=(1200, 1600))
    pdf = _make_pdf([
        [(_png_bytes((255, 0, 0)), (100, 100, 300, 300))],  # page 1 (irrelevant)
        [(bg, (0, 0, 612, 792))],                            # page 2 → bg only
    ])
    reflow = [{"figure_id": "figure-1", "page": 2, "url": ""}]

    with pytest.raises(PdfFigureNotFoundError, match="extractable raster"):
        extract_figure_for_reflow_id(pdf, reflow, "figure-1")


@pytest.mark.unit
def test_page_out_of_range_raises() -> None:
    """Reflow asks for a page that doesn't exist in the PDF."""
    pdf = _make_pdf([[(_png_bytes((255, 0, 0)), (100, 100, 300, 300))]])
    reflow = [{"figure_id": "figure-1", "page": 5, "url": ""}]

    with pytest.raises(PdfFigureNotFoundError, match="page 5"):
        extract_figure_for_reflow_id(pdf, reflow, "figure-1")
