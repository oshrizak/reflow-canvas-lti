# Braille (BRF) production

The BRF output aims to match what a transcriber would produce by hand in
Duxbury using the **English (UEB) — BANA** template, which is what US
alternate-media production manuals typically specify.

## Pipeline

```
canonical HTML
  -> blocks           (connector/canvas/braille.py)
       headings, paragraphs, list items, table rows, figures
       figure caption emitted before the description
       [Page n] markers become page-number blocks
       maths and mhchem runs tagged separately from prose
  -> translation      liblouis lou_translate
       en-ueb-g2.ctb                    prose
       nemeth.ctb / en-ueb-math.ctb     expressions
  -> layout           (connector/canvas/braille_layout.py)
       centred headings, 3-1 paragraphs, 1-3 lists
       40 cells x 25 lines, form feed between pages
  -> BRF
```

## Why the layout is ours and not liblouisutdml's

The obvious choice is `file2brl` (liblouisutdml), the open-source
counterpart to Duxbury. It was tried first and does not work on the
Debian packaging this runs on:

- its own semantic-action files (`html.sem` and the rest) fail to compile
  against the shipped binary — `list`, `table` and `trnote` actions "not
  recognized";
- the result is **exit status 0 with zero bytes of output**, in every
  input mode and with every bundled `.sem` file;
- `liblouisutdml.ini` also references `nemeth.ctb`, which Debian's
  `liblouis-data` does not ship.

Silent success is the worst failure mode available: a pipeline that
trusts an exit code produces empty documents and reports health. So the
BRF pipeline uses `lou_translate` — which is reliable and does the part
we genuinely cannot do ourselves — and applies the page layout in Python,
where it is deterministic and testable.

If a future image ships a working liblouisutdml, `build_braille_xml()`
still produces the XML it expects.

## Why prose and maths use different tables

This is the single most important property of the pipeline, and getting
it wrong is invisible to a sighted reviewer.

Nemeth is a mathematical notation. A document that contains one equation
must not have its *prose* transcribed in it. An earlier implementation
detected maths anywhere in a document and pushed the whole thing — every
paragraph of English — through `nemeth.ctb`; where that table exists the
output is unreadable, and where it doesn't the translation fails outright.

`resolve_math_table()` probes the image and prefers `nemeth.ctb`, falling
back to `en-ueb-math.ctb` (UEB Technical). BANA permits either alongside
UEB prose. Probing rather than hardcoding is deliberate: naming a table
that isn't installed is exactly how the previous version broke.

Chemistry (`\ce{...}`) is expanded by `connector/canvas/chemistry.py`
before translation, so `\ce{H2O}` reaches the maths table as a subscripted
formula rather than the literal characters `H`, `2`, `O`.

## Requirements from the production manual

| Requirement | Where it is implemented |
|---|---|
| UEB, not pre-2016 EBAE | `en-ueb-g2.ctb` in `render_brf` |
| Maths in a maths code, prose in UEB | `resolve_math_table()`, per-run tables |
| Heading structure preserved | `_BlockBuilder` maps `h1`–`h6` to heading blocks |
| Bullets and numbering preserved | `li` becomes `list_item`, 1-3 indent |
| Figure text *before* the graphic | `_flush_figure` defers image output to `</figure>` |
| Graphic descriptions included | `alt` becomes "Image description: …"; a missing one is announced, not dropped |
| Print page numbers preserved | `[Page n]` → right-aligned page-number line |
| No words broken across lines or pages | `_wrap` never splits a word |
| 40 cells × 25 lines | `layout_blocks(cells_per_line=, lines_per_page=)` |
| Braille page numbers | bottom-right cell of every page |

## Verification

Two test modules, both runnable without liblouis installed:

- `tests/unit/test_braille_conformance.py` — structure extraction: what
  survives from the HTML, and that maths is separated from prose.
- `tests/unit/test_braille_layout.py` — typesetting: indents, wrapping,
  pagination, page numbers. The translator is stubbed with identity so
  the assertions are about layout alone.

If you change this pipeline, read those failures carefully. Nobody
reviewing a BRF by eye will catch what they catch.

## Known limitations

Print page numbers only appear if the source markdown carries `[Page n]`
markers; Reflow does not currently emit them for every document.

Table rows are linearised, not rendered as braille tables. A real BANA
table format (columns, guide dots) is a larger piece of work.

The output has been verified structurally but not yet read by a braille
reader. Before relying on it for a student, have someone in your
alternate-media office check a file.
