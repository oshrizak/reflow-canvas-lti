"""Regression tests for the Tagged PDF output.

The Searchable PDF endpoint promises a PDF with a real structure tree
(headings, paragraphs, lists, tables — what Acrobat's Accessibility
panel reads). WeasyPrint only emits that tree when ``pdf_variant`` is
passed correctly; an earlier iteration silently dropped the option and
shipped a PDF with no tags at all. This test pins the contract.

We use ``pikepdf`` to parse the PDF rather than byte-grep because
WeasyPrint compresses the catalog inside an object stream — a naive
``in pdf_bytes`` check misses the structure even when it's present.
``pikepdf`` ships as a transitive dependency of ocrmypdf.
"""

from __future__ import annotations

import io

import pikepdf
import pytest
from connector.canvas.alt_formats import render_tagged_pdf
from connector.canvas.markdown_to_html import RenderedPage


@pytest.mark.unit
def test_render_tagged_pdf_emits_pdf_ua_structure_tree() -> None:
    """The generated PDF must carry the catalog entries Acrobat reads
    to populate its Accessibility Tags panel.

    Each assertion guards against a different way ``pdf_variant`` can
    silently slip out (kwarg name change, options-dict regression,
    WeasyPrint API drift).
    """
    page = RenderedPage(
        title="Test doc",
        html=(
            "<h1>A heading</h1>"
            "<p>A paragraph with <strong>emphasis</strong>.</p>"
            "<h2>Subheading</h2>"
            "<ul><li>One</li><li>Two</li></ul>"
        ),
    )
    pdf_bytes = render_tagged_pdf(page)

    # PDF/UA-1 mandates PDF 1.7.
    assert pdf_bytes.startswith(b"%PDF-1.7"), "PDF/UA-1 mandates PDF 1.7"

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        catalog = pdf.Root
        # Catalog must reference the structure root and mark the doc
        # as a Tagged PDF; Acrobat checks both before reading the tree.
        assert "/StructTreeRoot" in catalog, (
            "no /StructTreeRoot in catalog: pdf_variant kwarg was "
            "dropped, output has no tags (this is the regression we're "
            "guarding against)"
        )
        assert "/MarkInfo" in catalog, "no /MarkInfo entry on catalog"
        # ``Marked`` is a PDF boolean; pikepdf normalizes to a Python
        # ``True`` regardless of whether WeasyPrint wrote ``true`` or
        # ``True`` into the source bytes.
        assert bool(catalog["/MarkInfo"]["/Marked"]) is True, (
            "MarkInfo present but Marked != true"
        )
        # Acrobat refuses to show the doc title from the metadata until
        # this is on; WeasyPrint sets it for pdf/ua-1.
        assert "/ViewerPreferences" in catalog
        assert catalog["/ViewerPreferences"].get("/DisplayDocTitle") is True

        # Walk the structure tree to confirm semantic tags actually
        # made it in (not just an empty StructTreeRoot).
        struct_root = catalog["/StructTreeRoot"]
        assert "/K" in struct_root
        tags_found = _collect_struct_tags(struct_root)
        assert "H1" in tags_found, f"no H1 in struct tree (found: {tags_found})"
        assert "H2" in tags_found, f"no H2 in struct tree (found: {tags_found})"
        assert "P" in tags_found, f"no P in struct tree (found: {tags_found})"
        assert "L" in tags_found, f"no L (list) in struct tree (found: {tags_found})"


@pytest.mark.unit
def test_standalone_figures_are_siblings_of_paragraphs_not_children() -> None:
    """PDF/UA-1 requires standalone figures to sit at the same level as
    paragraphs in the structure tree. CommonMark wraps a markdown
    standalone image in a ``<p>`` and that maps to ``Figure`` nested
    inside ``P`` — the renderer post-processes to ``<figure>`` to put
    it at the right level. This guards that mapping survives all the
    way through to the PDF."""
    # 1x1 transparent PNG as a data URI keeps the test offline and
    # deterministic — using a placeholder URL added a network dependency
    # (CI runners block egress) and the SSL handshake would fail.
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
        "C0lEQVR42mNgAAIAAAUAAarVyFEAAAAASUVORK5CYII="
    )
    page = RenderedPage(
        title="Figure placement test",
        html=(
            "<p>Some prose paragraph.</p>"
            f"<figure><img src='{tiny_png}' alt='caption'></figure>"
            "<p>Another paragraph.</p>"
        ),
    )
    pdf_bytes = render_tagged_pdf(page)
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        struct_root = pdf.Root["/StructTreeRoot"]
        # Walk top-level structure elements and check the Figure parent
        # is NOT a P element. (The Document is the only allowed parent.)
        figure_parents = _find_parents_of(struct_root, child_tag="Figure")
        assert figure_parents, "no Figure found in structure tree"
        assert "P" not in figure_parents, (
            f"Figure nested inside P (parents found: {figure_parents}) — "
            "the standalone-image-to-figure promotion regressed"
        )


def _find_parents_of(
    obj, *, child_tag: str, depth: int = 0, parents: set[str] | None = None
) -> set[str]:
    """Walk the tree collecting the ``/S`` of every node that has at
    least one direct child with structure type ``child_tag``."""
    if parents is None:
        parents = set()
    if depth > 50:
        return parents
    try:
        children = obj.get("/K")
        if children is None:
            return parents
        # ``/K`` may be a single Dictionary, a single int (MCID), or an
        # Array; pikepdf.Array isn't a Python list so a plain
        # ``isinstance(_, list)`` check would skip iteration. Normalize
        # via ``iter`` and ignore non-iterables.
        try:
            iter(children)
            iterable = list(children) if not isinstance(children, pikepdf.Dictionary) else [children]
        except TypeError:
            iterable = [children]
        for child in iterable:
            if isinstance(child, pikepdf.Dictionary):
                child_s = child.get("/S")
                if child_s is not None and str(child_s).lstrip("/") == child_tag:
                    parent_s = obj.get("/S")
                    if parent_s is not None:
                        parents.add(str(parent_s).lstrip("/"))
                _find_parents_of(
                    child, child_tag=child_tag, depth=depth + 1, parents=parents,
                )
    except (AttributeError, TypeError, KeyError):
        pass
    return parents


def _collect_struct_tags(obj, depth: int = 0, found: set[str] | None = None) -> set[str]:
    """Walk the StructElem tree gathering every ``/S`` (structure type)."""
    if found is None:
        found = set()
    if depth > 50:
        return found  # cycle guard
    try:
        s = obj.get("/S")
        if s is not None:
            found.add(str(s).lstrip("/"))
        children = obj.get("/K")
        if children is None:
            return found
        try:
            iter(children)
            iterable = (
                list(children)
                if not isinstance(children, pikepdf.Dictionary) else [children]
            )
        except TypeError:
            iterable = [children]
        for child in iterable:
            if isinstance(child, pikepdf.Dictionary):
                _collect_struct_tags(child, depth + 1, found)
    except (AttributeError, TypeError, KeyError):
        pass
    return found
