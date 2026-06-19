"""Unit tests for the math/chemistry detection + Braille pre-processing.

These drive the wiring that auto-enables MathJax in HTML output and
routes Braille through the Nemeth table on math-bearing documents.
"""

from __future__ import annotations

import pytest
from connector.canvas.alt_formats import (
    _strip_latex_delimiters,
    has_math_content,
)
from connector.canvas.markdown_to_html import RenderedPage


def _rp(body: str) -> RenderedPage:
    return RenderedPage(title="t", html=body)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Einstein wrote $E = mc^2$ in 1905.",          # inline $...$
        "Display equation: $$\\int_0^1 x dx$$.",        # display $$...$$
        "Using \\(x^2 + y^2 = r^2\\) for a circle.",    # \(...\) inline
        "Then \\[\\int f dx\\] for the integral.",      # \[...\] display
        "Water is \\ce{H2O} per IUPAC.",                # mhchem chemistry
        "Pressure was \\pu{101 kPa}.",                  # mhchem units
    ],
)
def test_has_math_content_detects_all_delimiter_flavors(body: str) -> None:
    """MathJax should auto-load for every common math/chemistry marker."""
    assert has_math_content(_rp(body)) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Plain prose with no math.",
        "A price of $5 and another $7 — dollar signs in money, not math.",
        # ↑ guarded by the inline pattern requiring no $ or newline inside.
        # ``$5 and another $7`` contains a space + 'a' between them so isn't
        # matched as $...$ either way.
        "Use the syntax `\\ce` in a code block",
        "",
    ],
)
def test_has_math_content_does_not_misfire_on_plain_prose(body: str) -> None:
    assert has_math_content(_rp(body)) is False


@pytest.mark.unit
def test_strip_latex_delimiters_inline_dollar() -> None:
    """``$E = mc^2$`` becomes `` E = mc^2 `` so Nemeth transcribes the math
    content rather than literal ``$`` characters."""
    out = _strip_latex_delimiters("Energy is $E = mc^2$ everywhere.")
    assert "$" not in out
    assert "E = mc^2" in out


@pytest.mark.unit
def test_strip_latex_delimiters_display_dollar() -> None:
    out = _strip_latex_delimiters("$$\\int_0^1 f(x) dx$$")
    assert "$$" not in out
    assert "\\int_0^1 f(x) dx" in out


@pytest.mark.unit
def test_strip_latex_delimiters_paren_and_bracket_forms() -> None:
    out = _strip_latex_delimiters("Try \\(a+b\\) and \\[c=d\\].")
    assert "\\(" not in out and "\\)" not in out
    assert "\\[" not in out and "\\]" not in out
    assert "a+b" in out
    assert "c=d" in out


@pytest.mark.unit
def test_strip_latex_delimiters_mhchem_unwraps() -> None:
    """``\\ce{H2O}`` becomes `` H2O `` — Nemeth has no notion of ``\\ce``
    so we surface the chemical formula directly to be transcribed."""
    out = _strip_latex_delimiters("Boil \\ce{H2O} to 100C.")
    assert "\\ce" not in out
    assert "H2O" in out


@pytest.mark.unit
def test_strip_latex_delimiters_leaves_non_math_text_alone() -> None:
    plain = "Just regular prose with no math at all."
    assert _strip_latex_delimiters(plain) == plain
