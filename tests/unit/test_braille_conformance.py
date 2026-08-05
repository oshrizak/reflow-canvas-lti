"""Braille conformance, against an alternate-media production manual.

Braille defects are invisible to the people reviewing this code. A
sighted reviewer opening a BRF sees plausible-looking ASCII either way,
so nothing about the output *looks* wrong when it is wrong — which is
precisely why the previous implementation shipped a whole-document Nemeth
translation for months. These tests are the only thing standing between a
regression and a student receiving an unreadable file.

They assert on the structured XML handed to ``file2brl`` rather than on
brailled bytes, because liblouis tables are not installed in CI and the
XML is where every decision this module makes is visible.

Requirements exercised here, each traceable to the manual:

  * heading structure preserved (it is the document's navigation)
  * bullets and numbering preserved as lists
  * figure caption *before* the graphic, description always present
  * print page numbers preserved as page markers
  * maths and chemistry as MathML, so Nemeth applies to expressions only
"""

from __future__ import annotations

import pytest
from connector.canvas.braille import (
    build_braille_xml,
    latex_to_mathml,
    render_brf,
)
from connector.canvas.markdown_to_html import RenderedPage


def _xml(html: str, title: str = "Doc") -> str:
    return build_braille_xml(RenderedPage(title=title, html=html))


# --- structure ---------------------------------------------------------


def test_headings_survive():
    """Headings are how a braille reader navigates a chapter.

    Flattening them to plain text — what the old pipeline did — removes
    the entire outline of the document.
    """
    xml = _xml("<h2>Learning Goals</h2><p>Body text.</p><h3>Supplies</h3>")

    assert "<h2>" in xml and "Learning Goals" in xml
    assert "<h3>" in xml and "Supplies" in xml


def test_lists_survive_as_lists():
    xml = _xml("<ul><li>First item</li><li>Second item</li></ul>")

    assert xml.count("<li>") == 2
    assert "<ul>" in xml


def test_ordered_lists_keep_their_type():
    """Numbered and bulleted lists are transcribed differently."""
    xml = _xml("<ol><li>Step one</li></ol>")

    assert "<ol>" in xml
    assert "<ul>" not in xml


def test_tables_survive():
    xml = _xml("<table><tr><th>Element</th><td>Carbon</td></tr></table>")

    assert "<table>" in xml
    assert "<th>" in xml and "<td>" in xml


# --- figures -----------------------------------------------------------


def test_figure_caption_precedes_the_description():
    """The manual is explicit: identifying text goes before the graphic.

    A reader who cannot see the image needs to know what is coming before
    the description arrives, not after.
    """
    xml = _xml(
        "<figure><figcaption>Figure 2. Proposed workflow.</figcaption>"
        '<img src="f2.png" alt="Flowchart of the characterisation steps">'
        "</figure>"
    )

    caption_at = xml.index("Figure 2.")
    description_at = xml.index("Flowchart of the characterisation steps")
    assert caption_at < description_at, "caption must come before the description"


def test_image_alt_text_is_included():
    """Alt text was dropped entirely by the old flatten-to-text approach,
    so a braille reader got no indication an image existed at all."""
    xml = _xml('<figure><img src="a.png" alt="Ribbon diagram of the enzyme"></figure>')

    assert "Ribbon diagram of the enzyme" in xml
    assert "Image description" in xml


def test_undescribed_image_is_announced_not_silently_dropped():
    """Silence would tell the reader nothing is there. Something is there,
    and its description is missing — which is a finding, not a non-event."""
    xml = _xml('<figure><img src="a.png" alt=""></figure>')

    assert "Image without a description" in xml


# --- print page numbers ------------------------------------------------


def test_print_page_markers_become_page_elements():
    """A braille reader has to find 'page 214' in the same book as the class."""
    xml = _xml("<p>text before [Page 214] text after</p>")

    assert "<pagenum>214</pagenum>" in xml
    assert "[Page 214]" not in xml


def test_roman_numeral_page_markers_are_handled():
    """Front matter is numbered in roman numerals."""
    xml = _xml("<p>preface [Page xiv] continues</p>")

    assert "<pagenum>xiv</pagenum>" in xml


# --- maths and chemistry -----------------------------------------------


def test_inline_maths_becomes_mathml():
    """Nemeth applies to MathML expressions. Without this conversion the
    equation reaches the transcriber as literal dollar signs and
    backslashes."""
    xml = _xml(r"<p>The relation \(E = mc^2\) holds.</p>")

    assert "<math" in xml
    assert "\\(" not in xml and "$" not in xml


def test_display_maths_becomes_mathml():
    xml = _xml(r"<p>$$\int_0^1 x\,dx$$</p>")

    assert "<math" in xml
    assert "$$" not in xml


def test_prose_around_maths_stays_prose():
    """The bug this module was written to fix.

    The old code sent documents containing any maths — every paragraph of
    English in them — through the Nemeth table. Prose must remain outside
    the maths elements so it is transcribed in UEB.
    """
    xml = _xml(r"<p>Einstein showed that \(E = mc^2\) in 1905.</p>")

    before = xml.split("<math")[0]
    assert "Einstein showed that" in before, "prose must sit outside <math>"
    assert "in 1905" in xml.split("</math>")[-1], "prose must resume after </math>"


def test_chemistry_is_converted_not_passed_through_literally():
    r"""``\ce{H2O}`` must reach Nemeth as a subscripted formula.

    Passed through literally it transcribes as the characters H, 2, O —
    which is not what the formula means.
    """
    xml = _xml(r"<p>Water is \ce{H2O} at room temperature.</p>")

    assert "<math" in xml
    assert "\\ce{" not in xml
    assert "<msub" in xml or "_" in xml, "the subscript must survive conversion"


def test_unconvertible_maths_is_preserved_as_text():
    """Never drop an equation.

    A failed conversion still has to leave the expression in the document:
    this may be the student's only copy, and a missing equation is worse
    than an awkwardly transcribed one.
    """
    out = latex_to_mathml(r"\(\begin{unknownenv} \garbage \end{unknownenv}\)")

    assert "<math" in out
    assert out.strip() != ""


# --- safety ------------------------------------------------------------


def test_scripts_and_styles_never_reach_the_transcriber():
    xml = _xml("<p>Real text</p><script>alert(1)</script><style>p{color:red}</style>")

    assert "alert(1)" not in xml
    assert "color:red" not in xml
    assert "Real text" in xml


def test_title_becomes_the_document_heading():
    xml = _xml("<p>Body</p>", title="Module 1 Overview")

    assert "Module 1 Overview" in xml
    assert "<h1>" in xml


def test_missing_braille_tooling_fails_with_an_actionable_message(monkeypatch):
    """An operator reading this error must learn what to install."""
    monkeypatch.setattr("connector.canvas.braille.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError) as excinfo:
        render_brf(RenderedPage(title="t", html="<p>hello</p>"))

    message = str(excinfo.value)
    assert "liblouisutdml" in message
    assert "liblouis" in message
