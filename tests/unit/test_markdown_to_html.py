"""Unit tests for the markdown → HTML renderer's structural fixes.

The renderer adds a post-processing step that lifts standalone images
out of the CommonMark-wrapped ``<p>`` so they become ``<figure>``
elements. Without this, the Tagged PDF structure tree gets ``Figure``
nested inside ``P`` — PDF/UA-1 requires them at the same level for
standalone images.
"""

from __future__ import annotations

import pytest
from connector.canvas.markdown_to_html import render


@pytest.mark.unit
def test_standalone_image_is_promoted_to_figure() -> None:
    """A markdown line that's just an image must render as ``<figure>``,
    not wrapped in a ``<p>``. This is the fix that puts ``Figure`` as a
    sibling of ``P`` in the PDF structure tree."""
    md = "Some prose.\n\n![Alt text](http://example.com/img.png)\n\nMore prose."
    out = render(md, title="t").html
    assert "<figure>" in out
    assert "<figure><img" in out
    # The CommonMark wrapper is gone.
    assert "<p><img" not in out


@pytest.mark.unit
def test_inline_image_stays_inside_paragraph() -> None:
    """An image embedded in flowing prose belongs INSIDE the paragraph
    (PDF/UA-correct for inline figures). Don't promote it."""
    md = "A paragraph with ![alt](x.png) image inside text."
    out = render(md, title="t").html
    # Must remain inside the surrounding <p>.
    assert "<p>A paragraph with <img" in out
    # And not get promoted to a top-level figure.
    assert "<figure>" not in out


@pytest.mark.unit
def test_multiple_standalone_images_all_get_promoted() -> None:
    md = (
        "![one](a.png)\n\nText.\n\n![two](b.png)\n\nMore text.\n\n![three](c.png)"
    )
    out = render(md, title="t").html
    assert out.count("<figure>") == 3
    # Inline-style nested <p><img> from CommonMark should be fully gone.
    assert "<p><img" not in out


@pytest.mark.unit
def test_promotion_preserves_alt_text() -> None:
    """The alt attribute (used by screen readers AND copied into the
    PDF ``Figure`` alt) must survive the rewrap unchanged."""
    md = "![A useful description of the figure](http://x/img.png)"
    out = render(md, title="t").html
    assert 'alt="A useful description of the figure"' in out
