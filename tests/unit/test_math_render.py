"""Unit tests for the server-side LaTeX → inline SVG pipeline.

These cover what the Tagged PDF endpoint relies on:
    * ``preprocess_chemistry`` handles the common mhchem subset
      faculty actually puts in STEM PDFs.
    * ``render_latex_to_svg_data_uri`` returns a usable data URI for
      valid input and ``None`` for unparseable input (so the caller
      can fall back to leaving the LaTeX text in place).
    * ``mathify_html`` replaces every supported delimiter flavor with
      ``<img>`` carrying the original LaTeX as ``alt`` text — the
      tagged PDF then has both the rendered glyphs and the accessible
      math source for screen readers.
"""

from __future__ import annotations

import pytest
from connector.canvas.math_render import (
    mathify_html,
    preprocess_chemistry,
    render_latex_to_svg_data_uri,
)


@pytest.mark.unit
def test_preprocess_chemistry_digit_subscripts() -> None:
    """``H2O`` -> ``H_{2}O`` so matplotlib renders the subscript."""
    assert preprocess_chemistry("H2O") == "H_{2}O"
    assert preprocess_chemistry("CaCl2") == "CaCl_{2}"
    assert preprocess_chemistry("Fe2O3") == "Fe_{2}O_{3}"


@pytest.mark.unit
def test_preprocess_chemistry_reaction_arrows() -> None:
    """``->`` and ``<->`` become real LaTeX arrows."""
    assert "\\rightarrow" in preprocess_chemistry("2H2 + O2 -> 2H2O")
    assert "\\rightleftharpoons" in preprocess_chemistry("N2 + 3H2 <-> 2NH3")


@pytest.mark.unit
def test_preprocess_chemistry_passes_through_simple_text() -> None:
    """No subscripts to add; output unchanged."""
    assert preprocess_chemistry("HCl") == "HCl"


@pytest.mark.unit
def test_render_latex_to_svg_returns_data_uri() -> None:
    """A valid LaTeX snippet renders to a base64 data URI."""
    uri = render_latex_to_svg_data_uri("E = mc^2")
    assert uri is not None
    assert uri.startswith("data:image/svg+xml;base64,")


@pytest.mark.unit
def test_render_latex_to_svg_returns_none_for_garbage() -> None:
    """mathtext can't parse this; we return None rather than crashing,
    so the caller leaves the original markup in place."""
    # Unbalanced braces — mathtext raises.
    uri = render_latex_to_svg_data_uri("\\frac{1}{")
    assert uri is None


@pytest.mark.unit
def test_mathify_inline_dollar() -> None:
    out = mathify_html("Energy is $E = mc^2$ everywhere.")
    assert "<img" in out
    assert 'class="math-inline"' in out
    # The alt text preserves the original LaTeX source for AT.
    assert 'alt="E = mc^2"' in out
    # The literal $ delimiters are gone — math is now an img.
    assert "$E" not in out


@pytest.mark.unit
def test_mathify_display_dollar() -> None:
    out = mathify_html("$$\\int_0^1 f(x) dx$$")
    assert 'class="math-display"' in out
    assert "$$" not in out


@pytest.mark.unit
def test_mathify_paren_and_bracket_forms() -> None:
    """Both ``\\(...\\)`` (inline) and ``\\[...\\]`` (display) get rendered."""
    out = mathify_html("Inline \\(a+b\\) and display \\[c=d\\].")
    assert out.count("<img") == 2
    assert 'class="math-inline"' in out
    assert 'class="math-display"' in out


@pytest.mark.unit
def test_mathify_chemistry() -> None:
    """``\\ce{H2O}`` becomes an img whose alt is the original mhchem source
    (faculty / AT can still read the un-preprocessed form)."""
    out = mathify_html("Water is \\ce{H2O}.")
    assert "<img" in out
    assert 'alt="H2O"' in out
    assert "\\ce" not in out


@pytest.mark.unit
def test_mathify_leaves_plain_prose_untouched() -> None:
    """No math markers, no changes — ``mathify_html`` isn't destructive."""
    plain = "<p>Just a paragraph with no math at all.</p>"
    assert mathify_html(plain) == plain


@pytest.mark.unit
def test_mathify_does_not_misfire_on_money_text() -> None:
    """``$5 and another $7`` is prose, not math. ``mathify_html`` must
    not turn it into an image (same guard as ``has_math_content``)."""
    text = "A price of $5 and another $7 — these are dollars."
    assert "<img" not in mathify_html(text)
