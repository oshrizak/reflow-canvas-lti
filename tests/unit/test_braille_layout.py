"""BANA page layout for braille.

Translation is liblouis's job; layout is ours, and it is the half nobody
can check by eye. A sighted reviewer opening a BRF sees plausible ASCII
whether the indentation, pagination and page numbering are right or
wrong, so these tests are the only real verification the formatting gets.

The translator is stubbed with identity so assertions are about layout
alone — indents, wrapping, page breaks, page numbers — and run in CI
without liblouis tables installed.
"""

from __future__ import annotations

from connector.canvas.braille_layout import (
    FORM_FEED,
    Block,
    Run,
    layout_blocks,
)


def _plain(text: str, is_math: bool) -> str:  # noqa: ARG001 — stub signature
    return text


def _lay(blocks, **kw):
    return layout_blocks(blocks, _plain, **kw)


def _para(text: str) -> Block:
    return Block(kind="paragraph", runs=[Run(text=text)])


# --- indentation -------------------------------------------------------


def test_paragraphs_use_three_one_indent():
    """Braille Formats: paragraph first line indented, runover at margin."""
    out = _lay([_para("alpha beta gamma delta epsilon")], cells_per_line=20)
    lines = [ln for ln in out.split("\n") if ln.strip()]

    assert lines[0].startswith("  alpha"), "first line indents two cells"
    assert not lines[1].startswith(" "), "runover returns to the margin"


def test_list_items_use_one_three_indent():
    """Lists invert the paragraph shape so the marker stands out."""
    out = _lay(
        [Block(kind="list_item", runs=[Run(text="alpha beta gamma delta epsilon")])],
        cells_per_line=20,
    )
    lines = [ln for ln in out.split("\n") if ln.strip()]

    assert not lines[0].startswith(" "), "first line sits at the margin"
    assert lines[1].startswith("  "), "runover indents two cells"


def test_headings_are_centred_and_followed_by_a_blank_line():
    out = _lay(
        [Block(kind="heading", level=1, runs=[Run(text="title")]), _para("body")],
        cells_per_line=20,
    )
    lines = out.split("\n")

    assert lines[0].startswith(" ") and lines[0].strip() == "title"
    assert lines[1] == "", "a heading needs air or it reads as body text"


# --- wrapping ----------------------------------------------------------


def test_words_are_never_split():
    """The manual forbids breaking a word — it interrupts reading."""
    out = _lay([_para("aaa bbbbbbbbbb ccc")], cells_per_line=12)

    for line in out.split("\n"):
        if line.strip().isdigit():
            continue  # braille page number, not content
        for word in line.split():
            assert word in ("aaa", "bbbbbbbbbb", "ccc"), f"word was split: {word!r}"


def test_overlong_word_is_kept_whole_rather_than_truncated():
    """Truncating would change what the document says."""
    out = _lay([_para("supercalifragilistic")], cells_per_line=10)

    assert "supercalifragilistic" in out


def test_lines_respect_the_cell_width():
    out = _lay([_para(" ".join(["ab"] * 40))], cells_per_line=20)

    for line in out.split("\n"):
        if line and line != FORM_FEED:
            assert len(line) <= 20, f"line exceeds the cell width: {line!r}"


# --- pagination --------------------------------------------------------


def test_pages_break_at_the_line_limit_with_a_form_feed():
    blocks = [_para(f"line{i}") for i in range(20)]
    out = _lay(blocks, cells_per_line=20, lines_per_page=5)

    assert FORM_FEED in out, "BRF pages are separated by a form feed"
    first_page = out.split(FORM_FEED)[0].rstrip("\n").split("\n")
    assert len(first_page) == 5, "a page holds exactly lines_per_page lines"


def test_every_page_ends_with_its_braille_page_number():
    blocks = [_para(f"line{i}") for i in range(12)]
    out = _lay(blocks, cells_per_line=20, lines_per_page=5)

    pages = out.split(FORM_FEED)
    for expected, page in enumerate(pages, start=1):
        last = page.rstrip("\n").split("\n")[-1]
        assert last.strip() == str(expected), (
            f"page {expected} must carry its number in the bottom-right cell"
        )
        assert last.endswith(str(expected)), "the number is right-aligned"


def test_content_is_not_lost_at_a_page_boundary():
    """Reserving the number line must not overwrite text."""
    blocks = [_para(f"word{i}") for i in range(9)]
    out = _lay(blocks, cells_per_line=20, lines_per_page=4)

    for i in range(9):
        assert f"word{i}" in out, f"word{i} was dropped at a page break"


# --- print page numbers ------------------------------------------------


def test_print_page_numbers_are_right_aligned_on_their_own_line():
    """A braille reader needs the page the rest of the class is on."""
    out = _lay(
        [_para("before"), Block(kind="page_number", page_label="214"), _para("after")],
        cells_per_line=20,
    )
    line = next(ln for ln in out.split("\n") if "214" in ln)

    assert line.rstrip().endswith("214")
    assert line.startswith(" "), "print page numbers are right-aligned"
    assert "before" not in line and "after" not in line


# --- maths -------------------------------------------------------------


def test_maths_runs_are_translated_with_the_maths_table():
    """The whole point of the rewrite: prose and maths use different tables."""
    seen: list[tuple[str, bool]] = []

    def spy(text: str, is_math: bool) -> str:
        seen.append((text, is_math))
        return text

    layout_blocks(
        [
            Block(
                kind="paragraph",
                runs=[
                    Run(text="Einstein showed"),
                    Run(text="E = mc^2", is_math=True),
                    Run(text="in 1905"),
                ],
            )
        ],
        spy,
    )

    assert ("Einstein showed", False) in seen
    assert ("E = mc^2", True) in seen, "the equation must use the maths table"
    assert ("in 1905", False) in seen, "prose after the equation stays prose"


def test_empty_document_produces_no_output():
    assert _lay([]) == ""
