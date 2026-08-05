"""BANA page layout for braille, done in Python.

The obvious way to format braille is liblouisutdml (``file2brl``), the
open-source counterpart to Duxbury. On the deployment this was written
for it does not work: its own semantic-action files fail to compile
against the shipped binary, so it reports success and emits an empty
file. That failure mode — exit status 0, zero bytes — is worse than a
crash, because a pipeline that trusts the exit code silently produces
nothing.

So the layout lives here instead. ``lou_translate`` is reliable and does
the one thing we genuinely cannot do ourselves: convert text to braille
cells using the UEB and maths tables. Everything after that is
deterministic typesetting, which is easier to own, test and reason about
than a dependency that lies about succeeding.

Layout follows the *Braille Formats* conventions an alternate-media
production manual assumes:

* 40 cells by 25 lines, form feed between pages.
* Headings centred when they fit, otherwise cell 1; blank line after.
* Paragraphs 3-1 — first line indented two cells, runover at the margin.
* List items 1-3 — first line at the margin, runover indented.
* Braille page number in the bottom-right cell of every page.
* Print page numbers on their own line, right-aligned, so a reader can
  find the same page as the rest of the class.
* Words are never split across a line or a page.

The translation function is injected so the whole module can be tested
without liblouis installed — which matters, because nobody reviewing a
braille file by eye will notice a layout regression.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CELLS_PER_LINE = 40
LINES_PER_PAGE = 25
FORM_FEED = "\f"


@dataclass
class Run:
    """A stretch of text sharing one braille table."""

    text: str
    is_math: bool = False


@dataclass
class Block:
    """One structural unit of the document.

    ``kind`` is one of: ``heading``, ``paragraph``, ``list_item``,
    ``table_row``, ``figure``, ``page_number``.
    """

    kind: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0          # heading depth, or list nesting
    page_label: str = ""    # print page number, for kind == "page_number"


# (first-line indent, runover indent), in cells, per BANA Braille Formats.
_INDENTS = {
    "heading": (0, 0),      # centred when it fits; margin otherwise
    "paragraph": (2, 0),    # 3-1 in one-based cell numbering
    "list_item": (0, 2),    # 1-3
    "table_row": (0, 2),
    "figure": (2, 0),
}


def _wrap(cells: str, width: int, first_indent: int, runover: int) -> list[str]:
    """Wrap translated braille without ever splitting a word.

    The production manual forbids breaking a word across a page because it
    interrupts reading; the same applies to lines. A word longer than the
    available width is placed alone and allowed to overflow rather than
    being silently truncated — losing characters would change what the
    document says.
    """
    words = [w for w in cells.split(" ") if w]
    if not words:
        return []
    lines: list[str] = []
    indent = first_indent
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) + indent <= width:
            current = candidate
            continue
        if current:
            lines.append(" " * indent + current)
            indent = runover
        current = word
    if current:
        lines.append(" " * indent + current)
    return lines


def _centre(cells: str, width: int) -> list[str]:
    if len(cells) >= width:
        return _wrap(cells, width, 0, 0)
    pad = (width - len(cells)) // 2
    return [" " * pad + cells]


def layout_blocks(
    blocks: list[Block],
    translate: Callable[[str, bool], str],
    *,
    cells_per_line: int = CELLS_PER_LINE,
    lines_per_page: int = LINES_PER_PAGE,
) -> str:
    """Translate and typeset ``blocks`` into a paginated BRF body.

    ``translate(text, is_math)`` returns braille cells for one run; the
    caller supplies it so this module never shells out and stays testable.
    """
    body: list[str] = []
    prev_kind = ""

    for block in blocks:
        if block.kind == "page_number":
            # Right-aligned on its own line: this is the *print* page the
            # sighted class is looking at, not the braille page.
            label = translate(f"page {block.page_label}", False)
            body.append(label.rjust(cells_per_line)[:cells_per_line])
            continue

        cells = " ".join(
            t for t in (translate(r.text, r.is_math) for r in block.runs) if t
        ).strip()
        if not cells:
            continue

        # A heading needs air around it or it reads as body text.
        if block.kind == "heading" and body and body[-1] != "":
            body.append("")

        if block.kind == "heading":
            body.extend(_centre(cells, cells_per_line))
            body.append("")
        else:
            first, runover = _INDENTS.get(block.kind, (0, 0))
            body.extend(_wrap(cells, cells_per_line, first, runover))

        prev_kind = block.kind

    if prev_kind:  # trim a trailing blank produced by a final heading
        while body and body[-1] == "":
            body.pop()

    return _paginate(body, cells_per_line, lines_per_page)


def _paginate(lines: list[str], width: int, per_page: int) -> str:
    """Break into pages, reserving the last line for the page number.

    A braille page number sits in the bottom-right cell. Reserving the
    line rather than overwriting content is what keeps text from being
    lost at a page boundary.
    """
    if not lines:
        return ""
    usable = max(1, per_page - 1)
    pages: list[str] = []
    for start in range(0, len(lines), usable):
        chunk = lines[start: start + usable]
        number = str(start // usable + 1)
        # Pad so the number always lands on the final line of the page.
        chunk = chunk + [""] * (usable - len(chunk))
        chunk.append(number.rjust(width)[:width])
        pages.append("\n".join(chunk))
    return (FORM_FEED + "\n").join(pages) + "\n"
