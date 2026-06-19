"""Server-side LaTeX math → inline SVG for Tagged PDF output.

WeasyPrint doesn't execute JavaScript, so MathJax can't render math
inside the tagged PDF the connector hands faculty when they click
Searchable PDF on a born-digital input. We pre-render each math span
server-side here, using matplotlib's ``mathtext`` (a LaTeX-subset
renderer bundled with matplotlib that does NOT require a system TeX
install), and embed the result as an inline ``<img>`` with a base64
``data:`` URI plus the LaTeX source as ``alt`` text.

Why ``alt`` text matters: tagged PDFs preserve the alt attribute on
images. A screen reader consuming the tagged PDF reads the LaTeX
source for the math image — which is the same fallback MathJax uses
for assistive tech. So the math is both visible (the SVG) and
audible (the alt text).

mhchem chemistry (``\\ce{...}``, ``\\pu{...}``) is the other thing
faculty in CSU East Bay's chem courses care about. The full mhchem
grammar is large; we handle the common cases (digit subscripts after
element symbols, reaction arrows) by translating to matplotlib's
LaTeX subset before rendering.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from html import escape as _esc

import matplotlib

# ``Agg`` is the non-interactive backend — no display server needed,
# safe inside a container. Set BEFORE any pyplot/Figure import.
matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402

logger = logging.getLogger(__name__)


# Same delimiter set ``has_math_content`` checks for, but capturing the
# inner LaTeX so we can convert it. Order is significant: chemistry
# preprocessor runs first (its braces ``{...}`` would otherwise eat
# into the inline ``$...$`` regex), then display math (``$$``, ``\[``),
# then inline math (``$``, ``\(``).
_DISPLAY_DOLLAR = re.compile(r"\$\$([\s\S]+?)\$\$")
_DISPLAY_BRACKET = re.compile(r"\\\[([\s\S]+?)\\\]")
_INLINE_DOLLAR = re.compile(r"\$(?!\s)([^\$\n]*?[^\s\$])\$")
_INLINE_PAREN = re.compile(r"\\\(([\s\S]+?)\\\)")
_CHEM_CE = re.compile(r"\\ce\{([^{}]+)\}")
_CHEM_PU = re.compile(r"\\pu\{([^{}]+)\}")


def render_latex_to_svg_data_uri(
    latex: str, *, display: bool = False, fontsize: int = 12
) -> str | None:
    """Render LaTeX math to an SVG data URI.

    ``display`` bumps the font size — matplotlib's mathtext doesn't
    accept ``\\displaystyle`` (it's LaTeX-proper, not part of the
    mathtext subset), so the visual distinction between inline and
    display is carried via font scale + CSS class on the resulting
    ``<img>``. Returns ``None`` on failure so the caller can keep
    the original LaTeX text in place rather than dropping the math
    from the page.
    """
    try:
        fig = Figure(figsize=(0.001, 0.001))
        fig.patch.set_alpha(0)
        text = f"${latex}$"
        effective_fontsize = int(fontsize * 1.4) if display else fontsize
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_axis_off()
        ax.text(0.5, 0.5, text, fontsize=effective_fontsize, ha="center", va="center")
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="svg",
            bbox_inches="tight",
            pad_inches=0.02,
            transparent=True,
        )
        svg = buf.getvalue()
        encoded = base64.b64encode(svg).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    except Exception as exc:  # noqa: BLE001 — mathtext raises many ad-hoc types
        logger.debug("latex SVG render failed for %r: %s", latex[:80], exc)
        return None


def preprocess_chemistry(ce_content: str) -> str:
    """Translate the common mhchem subset into matplotlib-friendly LaTeX.

    Handled:
      * Digit-after-element subscripts: ``H2O`` -> ``H_{2}O``,
        ``CaCl2`` -> ``CaCl_{2}``.
      * Reaction arrows: ``->`` -> ``\\rightarrow``,
        ``<->`` -> ``\\rightleftharpoons``.
      * Charges left as-is — they're already in ``^{...}`` form when
        Reflow's pipeline emits them.

    Not handled (will pass through as plain text, possibly mangled):
      * Stoichiometry coefficients prefixing formulas (``2H2O``)
      * Isotope mass-number / charge sandwiches (``^{14}C``)
      * Bond notation (``-``, ``=``, ``\\equiv``)
      * Phases (``(s)``, ``(aq)``)
    """
    s = ce_content
    # Equilibrium ``<->`` must run before the reaction arrow ``->`` or
    # the right half (``->``) gets gobbled by the simpler rule.
    s = re.sub(r"<->", r"\\rightleftharpoons ", s)
    s = re.sub(r"->", r"\\rightarrow ", s)
    s = re.sub(r"([A-Za-z\)\]])(\d+)", r"\1_{\2}", s)
    return s


def _replace_or_keep(
    match: re.Match[str],
    *,
    display: bool,
    chemistry: bool = False,
) -> str:
    """Return an ``<img>`` for the matched span; on render failure,
    keep the original markup so the page still shows the math text."""
    src = match.group(1)
    latex = preprocess_chemistry(src) if chemistry else src
    data_uri = render_latex_to_svg_data_uri(latex, display=display)
    if data_uri is None:
        return match.group(0)
    klass = "math-display" if display else "math-inline"
    return (
        f'<img class="{klass}" alt="{_esc(src)}" src="{data_uri}" '
        f'style="vertical-align: middle;" />'
    )


def mathify_html(html: str) -> str:
    """Replace LaTeX/mhchem spans in ``html`` with inline SVG ``<img>``.

    Idempotency: the produced ``<img>`` tags don't contain ``$`` or
    ``\\[`` markers, so re-running this on its own output is a no-op.
    """
    out = _CHEM_CE.sub(
        lambda m: _replace_or_keep(m, display=False, chemistry=True), html
    )
    out = _CHEM_PU.sub(
        lambda m: _replace_or_keep(m, display=False, chemistry=True), out
    )
    out = _DISPLAY_DOLLAR.sub(lambda m: _replace_or_keep(m, display=True), out)
    out = _DISPLAY_BRACKET.sub(lambda m: _replace_or_keep(m, display=True), out)
    out = _INLINE_DOLLAR.sub(lambda m: _replace_or_keep(m, display=False), out)
    out = _INLINE_PAREN.sub(lambda m: _replace_or_keep(m, display=False), out)
    return out
