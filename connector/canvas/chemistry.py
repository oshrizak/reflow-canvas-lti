"""mhchem chemistry notation -> plain LaTeX.

Split out of ``math_render`` so the transform can be used without pulling
in matplotlib. The braille pipeline needs exactly this function and
nothing else from that module; importing a plotting library to run a
handful of regexes is a cost paid on every braille request, and it turns
a missing graphics dependency into a failure to produce a student's
document.
"""

from __future__ import annotations

import re


def preprocess_chemistry(ce_content: str) -> str:
    """Translate the common mhchem subset into plain LaTeX.

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
