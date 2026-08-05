# Braille (BRF) production

The BRF output aims to match what a transcriber would produce by hand in
Duxbury using the **English (UEB) — BANA** template, which is what US
alternate-media production manuals typically specify.

Duxbury is proprietary and has no redistributable SDK. Its open-source
counterpart is **liblouisutdml** (`file2brl`), which formats a *document*
rather than translating a *string* — page layout, running heads, braille
and print page numbers, centred headings, indented lists, linearised
tables, and MathML through Nemeth.

## Pipeline

```
canonical HTML
  -> structured XML (connector/canvas/braille.py)
       headings, lists, tables kept as markup
       figure caption emitted before the description
       [Page n] markers -> <pagenum>
       LaTeX / mhchem -> MathML
  -> file2brl  (connector/canvas/braille_ueb_bana.cfg)
       literaryTextTable  en-ueb-g2.ctb   prose
       mathexprTable      nemeth.ctb      expressions
  -> BRF, 40 cells x 25 lines
```

## Why prose and maths use different tables

This is the single most important property of the pipeline, and getting it
wrong is invisible to a sighted reviewer.

Nemeth is a mathematical notation. A document that contains one equation
must not have its *prose* transcribed in it. An earlier implementation
detected maths anywhere in a document and pushed the whole thing —
every paragraph of English — through `nemeth.ctb`, producing a file a
braille reader could not read. The output still looked like plausible
ASCII to anyone checking it visually.

The correct treatment, per BANA's *Guidance for Transcription Using the
Nemeth Code within UEB Contexts*, is UEB throughout with Nemeth applied to
the expressions. `file2brl` does this when the maths arrives as MathML,
which is why the LaTeX in the canonical HTML is converted server-side —
MathJax only renders in a browser, and there is no browser here.

Chemistry (`\ce{...}`) is expanded by `connector/canvas/chemistry.py`
before conversion, so `\ce{H2O}` reaches Nemeth as a subscripted formula
rather than the literal characters `H`, `2`, `O`.

## Requirements from the production manual

| Requirement | Where it is implemented |
|---|---|
| UEB, not pre-2016 EBAE | `literaryTextTable en-ueb-g2.ctb` |
| Heading structure preserved | `_BrailleXmlBuilder` keeps `h1`–`h6` |
| Bullets and numbering preserved | `ul` / `ol` / `li` kept as markup |
| Figure text *before* the graphic | `_flush_figure` defers image output to `</figure>` |
| Graphic descriptions included | `alt` becomes "Image description: …"; a missing one is announced, not dropped |
| Print page numbers preserved | `[Page n]` → `<pagenum>` |
| No words broken across lines | wrapping never splits a word |
| 40 cells × 25 lines | `cellsPerLine` / `linesPerPage` |

## Degraded mode

If `file2brl` is missing the module falls back to unstructured
translation and logs a warning. The fallback still uses **UEB** for prose
and Nemeth only for maths runs, so it is wrong in *layout* rather than
wrong in *language*. Losing page structure is recoverable; handing a
student prose transcribed in a maths notation is not.

Install `liblouisutdml-bin` (already in the Dockerfile) to avoid this.

## Verification

`tests/unit/test_braille_conformance.py` asserts on the structured XML
rather than on brailled bytes, because liblouis tables are not installed
in CI and the XML is where every decision is visible.

If you change this pipeline, run those tests and read the failures
carefully. Nobody reviewing a BRF by eye will catch what they catch.

## Tuning

`connector/canvas/braille_ueb_bana.cfg` holds the formatter configuration.
Embossers vary; cells and lines per page can also be overridden per call
via `render_brf(..., cells_per_line=, lines_per_page=)`.
