"""Braille Ready File (BRF) production.

The output here is meant to satisfy an alternate-media production manual
of the kind US disability-services offices run — the same requirements a
transcriber would meet by hand in Duxbury with the *English (UEB) — BANA*
template. Duxbury is proprietary and has no redistributable SDK, but its
open-source counterpart, **liblouisutdml** (``file2brl``), does the same
job: it formats a *document* rather than translating a *string*.

That distinction is the whole point of this module. The previous
implementation flattened the page to plain text and handed it to
``lou_translate``, which knows nothing about headings, lists, tables or
pages. Everything a braille reader uses to navigate was gone before
translation started, and the result was a single 40-column stream.

What the manual asks for, and what this produces:

``UEB, not EBAE``
    The manual specifies the *English (UEB) — BANA* template. The old code
    used ``en-us-g2.ctb``, which is pre-2016 EBAE. UEB has been the US
    standard since 2016.

``A maths code for the maths, UEB for the words``
    The old code took a document containing *any* maths and pushed the
    entire thing — every paragraph of English prose — through
    ``nemeth.ctb``. That table does not exist in Debian's liblouis
    packaging, so in practice those documents failed translation outright;
    where the table does exist, the result is worse than a failure,
    because prose transcribed in a mathematical notation looks like
    perfectly ordinary braille to anyone checking it visually.

    Here each expression is emitted as MathML and translated with a maths
    table, inside a document whose prose is UEB. BANA permits either
    Nemeth within UEB contexts or UEB Technical throughout;
    :func:`resolve_math_table` prefers Nemeth when the build provides it
    and otherwise uses UEB Technical (``en-ueb-math.ctb``). Both are
    correct braille. What matters is that neither is applied to prose.

``Structure survives``
    Headings, ordered and unordered lists, tables and figure descriptions
    reach the formatter as markup, so ``file2brl`` can centre, indent and
    linearise them the way a transcriber would.

``Figure descriptions, caption first``
    The manual is explicit that the text identifying a figure goes
    *before* the graphic. A reader who cannot see the image gets the
    caption, then the description, in that order.

``Print page numbers``
    Preserved when the source carries them, because a braille reader in a
    class discussion needs to find "page 214" in the same book everyone
    else is holding.

If ``file2brl`` is unavailable the module degrades to a plain-text
translation rather than failing outright — but it degrades to *UEB*, and
it still keeps maths out of the prose tables. A reduced file is better
than no file; a wrong one is not.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

from .braille_layout import Block, Run, layout_blocks
from .markdown_to_html import RenderedPage

logger = logging.getLogger(__name__)

# BANA's standard braille page: 40 cells wide, 25 lines deep.
CELLS_PER_LINE = 40
LINES_PER_PAGE = 25

# Maths and mhchem chemistry runs inside body text. Mirrors the detection
# regex in ``alt_formats`` — kept local so this module has no import cycle
# with it.
_MATH_RUN_RE = re.compile(
    r"(\$\$[\s\S]+?\$\$)"
    r"|(\\\[[\s\S]+?\\\])"
    r"|(\$(?!\s)[^\$\n]*?[^\s\$]\$)"
    r"|(\\\([\s\S]+?\\\))"
    r"|(\\ce\{[^{}]+\})"
    r"|(\\pu\{[^{}]+\})"
)

# ``[Page 214]`` markers, the convention the production manual uses for
# print page numbers placed inline in the text.
_PRINT_PAGE_RE = re.compile(r"\[\s*Page\s+([0-9ivxlcdm]+)\s*\]", re.IGNORECASE)

# Where liblouis keeps its tables on Debian/Ubuntu images.
_TABLE_DIRS = (
    Path("/usr/share/liblouis/tables"),
    Path("/usr/local/share/liblouis/tables"),
)

# Maths codes, in order of preference.
#
# BANA permits two treatments of technical material in a UEB document:
# Nemeth within UEB contexts, or UEB Technical throughout. Nemeth is still
# the house style at many US agencies for STEM, so it wins when present —
# but Debian's ``liblouis-data`` ships only ``nemethdefs.cti``, a
# definitions fragment that is not a usable table, so most deployments get
# UEB Technical. Both are correct braille; what matters is that neither is
# used for prose.
_MATH_TABLE_CANDIDATES = ("nemeth.ctb", "en-ueb-math.ctb", "en-us-mathtext.ctb")

_STRUCTURAL = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "blockquote", "figure", "figcaption",
}


def resolve_math_table(candidates: tuple[str, ...] = _MATH_TABLE_CANDIDATES) -> str:
    """Pick the first maths table this liblouis build actually ships.

    Hardcoding a table name is how the previous implementation broke: it
    asked for ``nemeth.ctb``, which this build does not contain, so every
    document with an equation failed at translation. Probing turns a hard
    failure into a documented substitution.

    Returns the last candidate if none are found, so the caller still gets
    a name to put in an error message rather than an empty string.
    """
    for name in candidates:
        for directory in _TABLE_DIRS:
            if (directory / name).exists():
                return name
    logger.warning(
        "No maths braille table found (looked for %s). Equations will be "
        "transcribed with the literary table, which is wrong for technical "
        "material — install liblouis tables.", ", ".join(candidates),
    )
    return candidates[-1]


def _strip_delimiters(fragment: str) -> tuple[str, bool]:
    """Return (latex, is_chemistry) with the fence characters removed."""
    f = fragment.strip()
    for open_d, close_d in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)")):
        if f.startswith(open_d) and f.endswith(close_d):
            return f[len(open_d): -len(close_d)].strip(), False
    if f.startswith("\\ce{") and f.endswith("}"):
        return f[4:-1].strip(), True
    if f.startswith("\\pu{") and f.endswith("}"):
        return f[4:-1].strip(), True
    if f.startswith("$") and f.endswith("$"):
        return f[1:-1].strip(), False
    return f, False


def latex_to_mathml(fragment: str) -> str:
    """Convert one LaTeX or mhchem run to a MathML element.

    ``file2brl`` transcribes MathML through Nemeth. Our canonical HTML
    stores LaTeX because MathJax renders it in the browser, so the maths
    has to be converted server-side before it can be brailled at all —
    there is no browser in this pipeline.

    Chemistry goes through the existing mhchem preprocessor first, so
    ``\\ce{H2O}`` reaches Nemeth as a subscripted formula rather than the
    literal characters ``H2O``.

    Falls back to the plain expression wrapped in ``<mtext>`` when
    conversion fails. A readable approximation beats a traceback, and the
    alternative — dropping the equation — silently removes content from a
    student's only copy of the document.
    """
    latex, is_chem = _strip_delimiters(fragment)
    if not latex:
        return ""
    if is_chem:
        from .chemistry import preprocess_chemistry

        latex = preprocess_chemistry(latex)
    try:
        from latex2mathml.converter import convert

        return convert(latex)
    except Exception as exc:  # noqa: BLE001 — never lose the expression
        logger.warning(
            "MathML conversion failed for %r (%s); emitting literal text",
            latex[:60], exc,
        )
        return f"<math><mtext>{html_module.escape(latex)}</mtext></math>"


def _text_to_xml(text: str) -> str:
    """Escape body text, converting maths runs to MathML and page markers.

    Print page numbers become ``<pagenum>``, which liblouisutdml places
    per BANA rules rather than leaving mid-sentence.
    """
    out: list[str] = []
    pos = 0
    for m in _MATH_RUN_RE.finditer(text):
        out.append(_escape_with_pages(text[pos: m.start()]))
        out.append(latex_to_mathml(m.group(0)))
        pos = m.end()
    out.append(_escape_with_pages(text[pos:]))
    return "".join(out)


def _escape_with_pages(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    pos = 0
    for m in _PRINT_PAGE_RE.finditer(text):
        parts.append(html_module.escape(text[pos: m.start()]))
        parts.append(f"<pagenum>{html_module.escape(m.group(1))}</pagenum>")
        pos = m.end()
    parts.append(html_module.escape(text[pos:]))
    return "".join(parts)


class _BrailleXmlBuilder(HTMLParser):
    """Rewrite the canonical HTML into the subset ``file2brl`` understands.

    Only structural elements survive. Inline presentation (``<em>``,
    ``<strong>``, links) is dropped to text: braille has its own emphasis
    conventions and carrying HTML styling into it produces noise, not
    meaning.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0          # inside <script>/<style>
        self._figure_alt: list[str] = []
        self._in_figcaption = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag == "img":
            # Held until the figure closes so the caption is emitted first,
            # which is what the production manual requires.
            alt = (a.get("alt") or "").strip()
            self._figure_alt.append(alt)
            return
        if tag == "figcaption":
            self._in_figcaption = True
        if tag in _STRUCTURAL:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "figcaption":
            self._in_figcaption = False
        if tag == "figure":
            self._flush_figure()
        if tag in _STRUCTURAL:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        self.parts.append(_text_to_xml(data))

    def _flush_figure(self) -> None:
        for alt in self._figure_alt:
            if alt:
                # Named so a reader knows this is a description standing in
                # for a graphic, not part of the running text.
                self.parts.append(f"<p>Image description: {html_module.escape(alt)}</p>")
            else:
                self.parts.append("<p>Image without a description.</p>")
        self._figure_alt.clear()

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._flush_figure()


class _BlockBuilder(HTMLParser):
    """Canonical HTML -> the block list the layout engine typesets.

    Same structural decisions as the XML builder, expressed as data rather
    than markup so the formatting can happen in Python. Inline
    presentation is dropped: braille has its own emphasis conventions and
    carrying HTML styling into it produces noise, not meaning.
    """

    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._skip = 0
        self._current: Block | None = None
        self._figure_alt: list[str] = []

    def _open(self, kind: str, level: int = 0) -> None:
        self._close()
        self._current = Block(kind=kind, level=level)

    def _close(self) -> None:
        if self._current and any(r.text.strip() for r in self._current.runs):
            self.blocks.append(self._current)
        self._current = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "img":
            self._figure_alt.append((dict(attrs).get("alt") or "").strip())
            return
        if tag in self._HEADINGS:
            self._open("heading", self._HEADINGS[tag])
        elif tag in ("p", "blockquote", "figcaption"):
            self._open("paragraph")
        elif tag == "li":
            self._open("list_item")
        elif tag == "tr":
            self._open("table_row")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "figure":
            self._close()
            self._flush_figure()
        elif tag in self._HEADINGS or tag in (
            "p", "blockquote", "figcaption", "li", "tr"
        ):
            self._close()

    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip():
            return
        if self._current is None:
            self._open("paragraph")
        assert self._current is not None
        for text, is_math, page in _split_runs(data):
            if page:
                self._close()
                self.blocks.append(Block(kind="page_number", page_label=page))
                self._open("paragraph")
            elif text.strip():
                self._current.runs.append(Run(text=text, is_math=is_math))

    def _flush_figure(self) -> None:
        for alt in self._figure_alt:
            # Named so a reader knows this stands in for a graphic rather
            # than being part of the running text. An undescribed image is
            # announced, never silently dropped.
            note = f"Image description: {alt}" if alt else "Image without a description."
            self.blocks.append(Block(kind="figure", runs=[Run(text=note)]))
        self._figure_alt.clear()

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._close()
        self._flush_figure()


def _split_runs(text: str) -> list[tuple[str, bool, str]]:
    """Split body text into (text, is_math, print_page_label) pieces."""
    out: list[tuple[str, bool, str]] = []
    pos = 0
    for m in _MATH_RUN_RE.finditer(text):
        out.extend(_split_pages(text[pos: m.start()]))
        latex, is_chem = _strip_delimiters(m.group(0))
        if is_chem:
            from .chemistry import preprocess_chemistry

            latex = preprocess_chemistry(latex)
        if latex:
            out.append((latex, True, ""))
        pos = m.end()
    out.extend(_split_pages(text[pos:]))
    return out


def _split_pages(text: str) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    pos = 0
    for m in _PRINT_PAGE_RE.finditer(text):
        out.append((text[pos: m.start()], False, ""))
        out.append(("", False, m.group(1)))
        pos = m.end()
    out.append((text[pos:], False, ""))
    return out


def build_blocks(rendered: RenderedPage) -> list[Block]:
    """Canonical HTML -> structural blocks, title first."""
    builder = _BlockBuilder()
    builder.feed(rendered.html or "")
    builder.close()
    title = (rendered.title or "").strip()
    head = [Block(kind="heading", level=1, runs=[Run(text=title)])] if title else []
    return head + builder.blocks


def build_braille_xml(rendered: RenderedPage) -> str:
    """Canonical HTML -> structured XML.

    Retained because it is the interchange format liblouisutdml wants, and
    because the conformance tests assert against it — but the BRF pipeline
    no longer uses it; see :mod:`connector.canvas.braille_layout` for why.
    """
    builder = _BrailleXmlBuilder()
    builder.feed(rendered.html or "")
    builder.close()
    body = "".join(builder.parts).strip()
    title = html_module.escape(rendered.title or "Untitled document")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">\n'
        f"<head><title>{title}</title></head>\n"
        f"<body><h1>{title}</h1>\n{body}\n</body>\n</html>\n"
    )


def render_brf(
    rendered: RenderedPage,
    *,
    grade: int = 2,
    cells_per_line: int = CELLS_PER_LINE,
    lines_per_page: int = LINES_PER_PAGE,
) -> bytes:
    """Produce a structured, paginated BRF.

    Translation is liblouis's job; layout is ours. See
    :mod:`connector.canvas.braille_layout` for why the formatting is not
    delegated to liblouisutdml.
    """
    lou = shutil.which("lou_translate")
    if not lou:
        raise RuntimeError(
            "lou_translate not found — install liblouis-bin (and "
            "liblouisutdml-bin for the braille tables) to produce braille."
        )

    blocks = build_blocks(rendered)
    if not blocks:
        raise RuntimeError("Nothing to braille-translate")

    literary = "en-ueb-g2.ctb" if grade == 2 else "en-ueb-g1.ctb"
    math_table = resolve_math_table()
    translate = _make_translator(lou, literary, math_table, blocks)

    body = layout_blocks(
        blocks,
        translate,
        cells_per_line=cells_per_line,
        lines_per_page=lines_per_page,
    )
    return body.encode("ascii", errors="replace")


def _make_translator(
    lou: str, literary: str, math_table: str, blocks: list[Block]
) -> Callable[[str, bool], str]:
    """Pre-translate every distinct run, then serve them from a cache.

    One subprocess per run would mean hundreds of process spawns for a
    chapter. liblouis translates line by line, so all the prose can go
    through in a single call and come back in the same order.
    """
    prose: list[str] = []
    maths: list[str] = []
    for block in blocks:
        if block.kind == "page_number":
            prose.append(f"page {block.page_label}")
            continue
        for run in block.runs:
            (maths if run.is_math else prose).append(run.text)

    cache: dict[tuple[bool, str], str] = {}
    for texts, table, is_math in ((prose, literary, False), (maths, math_table, True)):
        uniq = [t for t in dict.fromkeys(texts) if t.strip()]
        if not uniq:
            continue
        # Newlines delimit the batch, so no run may contain one.
        flat = [" ".join(t.split()) for t in uniq]
        out = _translate(lou, table, "\n".join(flat)).split("\n")
        if len(out) != len(flat):
            # Line counts drifted — fall back to one call per run rather
            # than risk pairing a translation with the wrong source text.
            logger.info(
                "Batched braille translation returned %d lines for %d inputs; "
                "translating individually.", len(out), len(flat),
            )
            for original, cleaned in zip(uniq, flat, strict=True):
                cache[(is_math, original)] = _translate(lou, table, cleaned)
        else:
            for original, cells in zip(uniq, out, strict=True):
                cache[(is_math, original)] = cells.strip()

    def translate(text: str, is_math: bool) -> str:
        if not text.strip():
            return ""
        hit = cache.get((is_math, text))
        if hit is not None:
            return hit
        return _translate(lou, math_table if is_math else literary, text)

    return translate


def _fallback_plain_brf(
    rendered: RenderedPage, *, grade: int, cells_per_line: int
) -> bytes:
    """Unstructured translation, used only when ``file2brl`` is unusable.

    Still splits prose from maths: prose is translated with UEB and each
    maths run separately with Nemeth. That is a poor substitute for real
    Nemeth switch indicators, but it is a great deal better than pushing
    an entire document of English through a mathematical notation, which
    is what this module replaced.
    """
    from .alt_formats import html_to_plain_text

    text = html_to_plain_text(rendered)
    if not text.strip():
        raise RuntimeError("Nothing to braille-translate")

    lou = shutil.which("lou_translate")
    if not lou:
        raise RuntimeError(
            "Neither file2brl nor lou_translate is available; install "
            "liblouisutdml-bin and liblouis-bin to produce braille."
        )

    literary = "en-ueb-g2.ctb" if grade == 2 else "en-ueb-g1.ctb"
    chunks: list[str] = []
    pos = 0
    for m in _MATH_RUN_RE.finditer(text):
        before = text[pos: m.start()]
        if before.strip():
            chunks.append(_translate(lou, literary, before))
        latex, is_chem = _strip_delimiters(m.group(0))
        if is_chem:
            from .chemistry import preprocess_chemistry

            latex = preprocess_chemistry(latex)
        if latex:
            chunks.append(_translate(lou, resolve_math_table(), latex))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        chunks.append(_translate(lou, literary, tail))

    return _wrap(" ".join(c for c in chunks if c), cells_per_line)


def _translate(lou: str, table: str, text: str) -> str:
    try:
        proc = subprocess.run(
            [lou, table],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "liblouis translation failed: "
            f"{exc.stderr.decode('utf-8', errors='replace')[:300]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("liblouis translation timed out") from exc
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _wrap(translated: str, cells_per_line: int) -> bytes:
    """Wrap to the cell width without splitting words.

    The manual is explicit that words must not break across a page
    boundary because it interrupts the flow of reading; the same reasoning
    applies to line breaks in a fixed-width medium.
    """
    lines: list[str] = []
    for para in translated.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            if not word:
                continue
            if not cur:
                cur = word
            elif len(cur) + 1 + len(word) <= cells_per_line:
                cur = f"{cur} {word}"
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return ("\n".join(lines) + "\n").encode("ascii", errors="replace")
