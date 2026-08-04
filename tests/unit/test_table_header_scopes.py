"""Table headers carry ``scope`` in the Canvas page body.

A ``<th>`` with no ``scope`` leaves a screen reader guessing which cells a
header governs — the association WCAG 2.2 SC 1.3.1 (Info and Relationships)
exists to make explicit. Markdown table syntax cannot express scope, so
mistune emits bare ``<th>`` and the structure the correction agents worked
out was being dropped at the very last step, in the renderer.

These also guard the claim the project makes publicly ("identifies header
row/column, adds scope attributes"), which was not true of the published
Canvas page before this.
"""

from __future__ import annotations

import re

from connector.canvas.markdown_to_html import render

TABLE_MD = """## Results

| Sample | Result | Notes |
|---|---|---|
| A | 12.4 | clean |
| B | 9.1 | rerun |
"""

# ``<th`` not followed by ``scope=`` before the closing ``>``. The trailing
# lookahead for whitespace or ``>`` stops this matching ``<thead>``.
BARE_TH = re.compile(r"<th(?=[\s>])(?![^>]*\bscope=)[^>]*>", re.IGNORECASE)


def test_column_headers_get_scope_col():
    html = render(TABLE_MD, title="T").html
    assert html.count('scope="col"') == 3


def test_no_header_cell_is_left_without_scope():
    html = render(TABLE_MD, title="T").html
    assert BARE_TH.findall(html) == []


def test_data_cells_are_untouched():
    """scope belongs on headers only; putting it on <td> is invalid."""
    html = render(TABLE_MD, title="T").html
    assert "<td scope" not in html
    assert html.count("<td>") == 6


def test_existing_scope_is_not_doubled():
    """If a future renderer emits scope itself, this must be a no-op."""
    html = render(TABLE_MD, title="T").html
    for match in re.findall(r"<th[^>]*>", html):
        assert match.count("scope=") <= 1, match


def test_table_without_headers_is_unaffected():
    html = render("Just a paragraph, no table.", title="T").html
    assert "<table" not in html
    assert "scope=" not in html


def test_output_stays_a_body_fragment():
    """Canvas page bodies must not carry document-level markup.

    Canvas sanitises <html>/<head>/<style>/<script> out of wiki page bodies.
    Since the bridge now writes this HTML straight into the page rather than
    linking out to it, emitting any of those would silently lose content.
    """
    html = render(TABLE_MD, title="T").html
    for tag in ("<html", "<head", "<body", "<style", "<script", "<link", "<meta"):
        assert tag not in html.lower(), f"{tag} would be stripped by Canvas"
