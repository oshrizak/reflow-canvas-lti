"""Unit tests for the PDF text-layer detector that routes Searchable PDF.

The endpoint picks between ocrmypdf (image-only scan) and WeasyPrint
tagged-PDF (born-digital) based on whether the source already has a
text layer. Mis-classifying a born-digital PDF as image-only would
hand faculty an identical-looking 'searchable' download — exactly the
bug this change exists to fix.
"""

from __future__ import annotations

import fitz
import pytest
from connector.canvas.alt_formats import pdf_has_text_layer


def _pdf_with_text(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


def _empty_pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=612, height=792)
    out = doc.tobytes()
    doc.close()
    return out


@pytest.mark.unit
def test_born_digital_pdf_is_detected_as_having_text() -> None:
    """A PDF generated from a text source has a text layer; the endpoint
    should route it through WeasyPrint, not ocrmypdf."""
    pdf = _pdf_with_text("Lorem ipsum dolor sit amet.")
    assert pdf_has_text_layer(pdf) is True


@pytest.mark.unit
def test_image_only_pdf_is_detected_as_having_no_text() -> None:
    """A PDF with no text layer (closest stand-in we can build without
    embedding an actual raster scan) should route through OCR."""
    pdf = _empty_pdf(pages=2)
    assert pdf_has_text_layer(pdf) is False


@pytest.mark.unit
def test_mixed_pdf_is_treated_as_having_text() -> None:
    """If any page has text the doc is treated as born-digital — even a
    partial text layer is enough to get the tagged-PDF path, which is
    the one with the structure tree."""
    doc = fitz.open()
    doc.new_page(width=612, height=792)               # blank
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "Page two has text.")
    pdf = doc.tobytes()
    doc.close()

    assert pdf_has_text_layer(pdf) is True
