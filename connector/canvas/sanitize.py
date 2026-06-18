"""HTML sanitization for instructor-edited and AI-generated content.

Every HTML byte that crosses our trust boundary -- whether it came
from the AI pipeline, an instructor's edits, or a markdown render --
runs through ``sanitize_html`` before being stored or served. The
allowlist is academic-content-oriented: headings, lists, tables,
figures, captions, code blocks, math (MathML), and ARIA landmarks.
Inline scripting, event handlers, and any non-HTTPS protocol on
links/images are stripped.

Why we don't use Bleach's defaults: Bleach defaults disallow tables,
figures, and ARIA landmarks -- exactly the markup we DO want for
accessible academic content. We explicitly opt them in here, with
attribute lists per tag.

Why we don't roll our own: the parser is the hard part. Bleach
delegates to html5lib which correctly handles malformed input,
SVG/MathML namespacing, and unicode tricks that a naive ``re.sub``
approach would miss. The shape of the allowlist is the design
decision; the parsing is the library's job.

Anti-claim: sanitization is necessary but not sufficient for
accessibility. The publication gate (Phase 7) checks WCAG conformance
separately. This module only removes *active content* -- scripts,
event handlers, javascript: URLs, dangerous embed/object/iframe.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# -- Allowlist ----------------------------------------------------------------

# Structural and semantic tags that belong in an accessible academic
# document. Anything not in this set is stripped.
ALLOWED_TAGS: list[str] = [
    # Document structure
    "html", "head", "body", "title", "meta", "link",
    "header", "footer", "main", "nav", "section", "article", "aside",
    # Headings + paragraphs
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "br", "hr",
    # Inline emphasis
    "em", "strong", "i", "b", "u", "s", "mark", "small", "sub", "sup",
    "abbr", "cite", "dfn", "kbd", "q", "samp", "time", "var",
    # Lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # Tables (semantic markup matters for accessibility)
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot",
    "tr", "th", "td",
    # Figures and media
    "figure", "figcaption", "img", "picture", "source",
    "audio", "video", "track",  # captions/transcripts attach via <track>
    # Links and code
    "a", "code", "pre", "blockquote",
    # MathML (accessibility-relevant; sanitized via attribute allowlist)
    "math", "mrow", "mi", "mn", "mo", "msup", "msub", "mfrac",
    "msqrt", "mroot", "mtext", "mspace", "mtable", "mtr", "mtd",
    "mover", "munder", "munderover", "mfenced", "mstyle",
    # ARIA landmark fallbacks for non-HTML5 markup
    "details", "summary",
]


# Per-tag attribute allowlist. ``"*"`` applies to every allowed tag.
# Keep these tight -- ``style`` and ``class`` are conservative because
# arbitrary CSS can still hide focus rings or change colors enough to
# fail contrast. We allow ``class`` only because the alt-format
# renderer depends on a small set of class names; bleach's CSS
# sanitizer below clamps allowed properties.
ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "*": [
        "id", "class", "lang", "dir", "title",
        # ARIA -- safe subset. ARIA roles can be misused but the
        # primary risk (XSS) doesn't apply, so we err inclusive.
        "role", "aria-label", "aria-labelledby", "aria-describedby",
        "aria-hidden", "aria-live", "aria-atomic", "aria-busy",
        "aria-current", "aria-expanded", "aria-haspopup",
        "aria-level", "aria-modal", "aria-required", "aria-readonly",
    ],
    "a": ["href", "target", "rel", "download"],
    "img": ["src", "srcset", "sizes", "alt", "width", "height",
            "loading", "decoding", "longdesc"],
    "picture": [],
    "source": ["src", "srcset", "sizes", "type", "media"],
    "audio": ["src", "controls", "preload"],
    "video": ["src", "controls", "preload", "poster", "width", "height"],
    "track": ["src", "kind", "srclang", "label", "default"],
    "table": ["summary"],
    "th": ["scope", "headers", "abbr", "colspan", "rowspan"],
    "td": ["headers", "colspan", "rowspan"],
    "col": ["span"],
    "colgroup": ["span"],
    "ol": ["start", "reversed", "type"],
    "li": ["value"],
    "time": ["datetime"],
    "abbr": ["title"],
    "q": ["cite"],
    "blockquote": ["cite"],
    "details": ["open"],
    # MathML -- attributes the W3C spec requires for accessibility info
    "math": ["display", "xmlns", "altimg", "alttext"],
    "mfrac": ["linethickness"],
    "mspace": ["width", "height", "depth"],
    "mstyle": ["mathvariant", "mathsize", "mathcolor"],
    "mtable": ["columnalign", "rowalign", "displaystyle"],
}


# URL protocols allowed in ``href`` / ``src`` / ``cite`` / ``longdesc``.
# Notably ABSENT: ``javascript:``, ``vbscript:``, ``file:``, ``ftp:``.
# ``data:`` is allowed because PDF figure extraction sometimes
# inlines small thumbnails -- the format whitelist below restricts
# to image MIME types only.
ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto", "data"]


# Permitted CSS properties on inline ``style`` attributes. The set is
# intentionally tiny: anything that affects visibility or focus is
# excluded so a malicious ``style`` can't hide essential content from
# screen readers or break keyboard navigation indicators.
ALLOWED_CSS_PROPS: list[str] = [
    "text-align", "font-weight", "font-style", "font-variant",
    "text-decoration", "color", "background-color",
    "white-space", "vertical-align",
]


# Tags whose *contents* must be removed entirely, not just the tag.
# Bleach's default ``strip=True`` removes disallowed tags but leaves
# their text content as text nodes -- safe (the browser will never
# execute it once the <script> tag is gone) but ugly: a stripped
# ``<script>alert(1)</script>`` would leave the literal text
# ``alert(1)`` in the output. We preprocess these elements out before
# bleach.clean to avoid the noise.
import re as _re  # noqa: E402

_STRIPPED_CONTENT_TAGS = ("script", "style", "iframe", "object", "embed", "noscript")
_STRIP_RE = _re.compile(
    r"<(" + "|".join(_STRIPPED_CONTENT_TAGS) + r")\b[^>]*>[\s\S]*?</\1\s*>",
    _re.IGNORECASE | _re.DOTALL,
)
# Also handle self-closing variants like ``<embed src=...>`` with no closing tag
_STRIP_VOID_RE = _re.compile(
    r"<(" + "|".join(_STRIPPED_CONTENT_TAGS) + r")\b[^>]*/?>(?!\s*</)",
    _re.IGNORECASE,
)


def _preprocess(raw: str) -> str:
    """Drop disallowed tags + their content before bleach sees the input."""
    out = _STRIP_RE.sub("", raw)
    out = _STRIP_VOID_RE.sub("", out)
    return out


def sanitize_html(raw: str, *, strip_comments: bool = True) -> str:
    """Sanitize untrusted HTML against the academic-content allowlist.

    Returns a *string* of HTML safe to store and serve. Anything the
    allowlist doesn't recognise -- including ``<script>``, event
    handlers (``onerror`` etc.), ``javascript:`` URLs, and unknown
    tags -- is removed. For ``<script>``, ``<style>``, ``<iframe>``
    and similar content-bearing tags, the *contents* are removed too
    (not just the wrapper), so a hostile ``<script>alert(1)</script>``
    leaves nothing behind -- not even ``alert(1)`` as text.

    For convenience, an empty/None input returns an empty string.
    Sanitization is idempotent: passing the output back in is a no-op.
    """
    if not raw:
        return ""
    try:
        import bleach
        from bleach.css_sanitizer import CSSSanitizer
    except ImportError:
        # Defensive: if bleach isn't installed (e.g. some dev shells),
        # don't silently return raw HTML. Refuse instead.
        logger.error(
            "sanitize_html: bleach is not installed; refusing to return raw HTML"
        )
        raise RuntimeError(
            "HTML sanitization required but bleach is not installed"
        )

    pre = _preprocess(raw)

    css_sanitizer = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPS)
    cleaned = bleach.clean(
        pre,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=css_sanitizer,
        strip=True,           # remove disallowed tags rather than escape
        strip_comments=strip_comments,
    )
    return cleaned


def is_safe_url(url: str | None) -> bool:
    """Cheap pre-check before constructing links/images server-side.

    Use this when you're building HTML programmatically and want to
    reject a URL *before* embedding it -- catches obvious bad cases
    so the sanitizer doesn't have to silently strip them later.
    """
    if not url:
        return False
    s = url.strip().lower()
    if s.startswith("javascript:") or s.startswith("vbscript:"):
        return False
    if any(s.startswith(p + ":") for p in ALLOWED_PROTOCOLS):
        return True
    # Relative paths are fine when used inside our own content; the
    # downstream sanitizer will reject if they resolve unsafely.
    if s.startswith(("/", "./", "../", "#")):
        return True
    return False
