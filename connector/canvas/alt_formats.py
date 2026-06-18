"""Alternative-format renderers.

Accessible HTML (produced by ``markdown_to_html.render``) is the
canonical accessible representation of a Reflow document. Every other
format — plain text, ePub, audio, translations, tagged PDF, etc. —
derives from that same RenderedPage, so all surfaces stay in sync. When
the HTML gets better (better alt text, better tables, better lang
attributes) every downstream format inherits the improvement.

Renderers are deliberately independent of FastAPI: they take a
RenderedPage in, return bytes (or a string) out, and never import the
server. That makes them trivial to unit test and to call from workers.
"""

from __future__ import annotations

import io
import logging
import re
import uuid

from .markdown_to_html import RenderedPage, render

logger = logging.getLogger(__name__)


def canonical_html(
    markdown: str,
    *,
    title: str,
    original_pdf_url: str | None = None,
    image_base_url: str | None = None,
) -> RenderedPage:
    """The single source of truth. All other formats derive from this."""
    return render(
        markdown,
        title=title,
        original_pdf_url=original_pdf_url,
        image_base_url=image_base_url,
    )


def html_full_document(rendered: RenderedPage, *, mathjax: bool = False) -> str:
    """Wrap the rendered body fragment in a full standalone HTML doc."""
    head_extras = ""
    if mathjax:
        head_extras = (
            '<script async="true" '
            'src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>'
        )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_escape(rendered.title)}</title>"
        "<style>body{font-family:Georgia,serif;max-width:48rem;margin:2rem auto;"
        "padding:0 1rem;line-height:1.65;color:#1d1d1d;}"
        "img{max-width:100%;height:auto;}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0;}"
        "th,td{border:1px solid #ccc;padding:0.5rem;text-align:left;}"
        "h1,h2,h3,h4{font-family:system-ui,sans-serif;}</style>"
        f"{head_extras}"
        f"</head><body><h1>{_escape(rendered.title)}</h1>{rendered.html}</body></html>"
    )


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&apos;": "'", "&#39;": "'", "&nbsp;": " "}


def html_to_plain_text(rendered: RenderedPage) -> str:
    """Strip tags from the rendered HTML to produce a clean text version."""
    text = _TAG_RE.sub(" ", rendered.html)
    for ent, repl in _ENTITIES.items():
        text = text.replace(ent, repl)
    text = _WS_RE.sub(" ", text).strip()
    return text


def render_epub(rendered: RenderedPage) -> bytes:
    """Convert the canonical HTML into an EPUB3 file."""
    try:
        from ebooklib import epub
        from ebooklib import utils as _ebu
    except ImportError as exc:
        raise RuntimeError(
            "ebooklib is not installed; add 'ebooklib' to dependencies"
        ) from exc

    # ebooklib >=0.18 (with lxml >=5) crashes on write: ``_get_nav`` runs
    # ``get_pages()`` over EVERY document item to build the EPUB3 page-list,
    # including the Nav/NCX items whose body is still empty at that point.
    # ``lxml.html.document_fromstring("")`` then raises "Document is empty",
    # surfacing as a 500 on the alt-format endpoint. Wrap ebooklib's
    # ``parse_html_string`` so an empty/whitespace body parses to an empty
    # document instead of throwing. Restored in ``finally`` so the patch is
    # scoped to this call and never leaks to other ebooklib users.
    _orig_parse = _ebu.parse_html_string

    def _safe_parse(s):
        is_empty = (
            s is None
            or (isinstance(s, (bytes, bytearray)) and not s.strip())
            or (isinstance(s, str) and not s.strip())
        )
        if is_empty:
            from lxml import html as _lh
            return _lh.fromstring("<html><body></body></html>")
        return _orig_parse(s)

    _ebu.parse_html_string = _safe_parse
    try:
        return _build_epub(epub, rendered)
    finally:
        _ebu.parse_html_string = _orig_parse


def _build_epub(epub, rendered: RenderedPage) -> bytes:
    book = epub.EpubBook()
    book.set_identifier(f"equalify-reflow-{uuid.uuid4()}")
    book.set_title(rendered.title or "Accessible Document")
    book.set_language("en")
    book.add_author("Equalify Reflow")

    chapter = epub.EpubHtml(
        title=rendered.title or "Document",
        file_name="content.xhtml",
        lang="en",
    )
    chapter.content = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<!DOCTYPE html><html xmlns=\"http://www.w3.org/1999/xhtml\" "
        "xmlns:epub=\"http://www.idpf.org/2007/ops\"><head>"
        f"<title>{_escape(rendered.title)}</title></head>"
        f"<body><h1>{_escape(rendered.title)}</h1>{rendered.html}</body></html>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (epub.Link("content.xhtml", rendered.title or "Document", "main"),)
    book.spine = ["nav", chapter]

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()


def _chunks(text: str, size: int) -> list[str]:
    """Split text on sentence boundaries first, then by size cap."""
    out: list[str] = []
    cur = ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for s in sentences:
        if len(cur) + len(s) + 1 <= size:
            cur = (cur + " " + s).strip() if cur else s
        else:
            if cur:
                out.append(cur)
            if len(s) <= size:
                cur = s
            else:
                # Hard split if a single sentence exceeds size
                for i in range(0, len(s), size):
                    out.append(s[i:i+size])
                cur = ""
    if cur:
        out.append(cur)
    return out


def render_audio_mp3(rendered: RenderedPage, voice: str = "Joanna") -> bytes:
    """Synthesize MP3 audio via Amazon Polly.

    Requires AWS credentials with ``polly:SynthesizeSpeech``. Raises
    ``RuntimeError`` with a friendly message if credentials/permissions
    aren't available so the endpoint can return 503.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed") from exc

    text = html_to_plain_text(rendered)
    if not text:
        raise RuntimeError("No text to synthesize")

    parts = _chunks(text, 2800)
    audio_chunks: list[bytes] = []
    polly = boto3.client("polly")
    for chunk in parts:
        try:
            resp = polly.synthesize_speech(
                Text=chunk,
                OutputFormat="mp3",
                VoiceId=voice,
                Engine="neural",
            )
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"Polly synthesis failed: {exc}") from exc
        audio_chunks.append(resp["AudioStream"].read())
    return b"".join(audio_chunks)


async def render_translation(
    rendered: RenderedPage,
    target_lang: str,
) -> RenderedPage:
    """Translate the accessible HTML into ``target_lang`` via the project AI.

    Returns a new RenderedPage with translated body. Requires the
    pipeline's AI provider to be configured (Anthropic API key or
    AWS Bedrock credentials).
    """
    try:
        from pydantic_ai import Agent

        from ..agents.model_factory import get_model_for_tier
        from ..agents.model_tiers import ModelTier
    except ImportError as exc:
        raise RuntimeError("PydanticAI is not available") from exc

    text = html_to_plain_text(rendered)
    if not text:
        raise RuntimeError("Nothing to translate")

    agent = Agent(
        get_model_for_tier(ModelTier.EFFICIENT),
        system_prompt=(
            "You are a translator preserving formatting and accessibility. "
            "Translate the provided text into the target language. Preserve "
            "headings, lists, and emphasis. Return ONLY the translated text."
        ),
    )
    result = await agent.run(f"Target language: {target_lang}\n\nText:\n{text}")
    translated = (result.output or "").strip()
    if not translated:
        raise RuntimeError("AI returned empty translation")
    # Wrap as a minimal HTML paragraph block; downstream uses html_full_document.
    paragraphs = "".join(f"<p>{_escape(p)}</p>" for p in translated.split("\n") if p.strip())
    return RenderedPage(
        title=f"{rendered.title} ({target_lang})",
        html=paragraphs,
    )


def _escape(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace("\"", "&quot;")
    )


def render_ocr_pdf(original_pdf_bytes: bytes, archival: bool = True) -> bytes:
    """OCR the original source PDF, returning a SEARCHABLE PDF.

    Uses ``ocrmypdf`` which wraps Tesseract + Ghostscript. When
    ``archival`` is True we ask for PDF/A output, which adds archival
    metadata (font embedding, color profiles) -- this is NOT the same
    as PDF/UA tagged structure. PDF/A != accessible. Real PDF/UA
    tagging (proper structure tree, alt text on figures, language
    markers, reading order metadata) requires a separate tagging pass
    that we have not yet implemented.

    The legacy ``tagged`` parameter name was misleading and has been
    renamed to ``archival`` so callers stop assuming this output meets
    PDF/UA accessibility requirements. If your code needs to assert
    accessible PDF output, do not call this function -- the proper
    accessible delivery is the converted Canvas HTML page.

    Raises ``RuntimeError`` with a friendly message if Tesseract or
    Ghostscript aren't installed in the container, so the endpoint can
    return 503.
    """
    # Keep ``tagged`` aliased for backward compatibility one cycle.
    # No call sites pass it positionally; remove after one release.
    import io
    try:
        import ocrmypdf
    except ImportError as exc:
        raise RuntimeError(
            "ocrmypdf not installed; add 'ocrmypdf' to dependencies and "
            "ensure tesseract-ocr + ghostscript are installed in the Dockerfile"
        ) from exc

    out_type = "pdfa" if archival else "pdf"
    in_buf = io.BytesIO(original_pdf_bytes)
    out_buf = io.BytesIO()
    # ``skip_text=True`` is the universal "make this searchable" mode: it OCRs
    # image-only pages and PASSES THROUGH pages that already have a text layer.
    # Critically, it also stops ocrmypdf from erroring on born-digital / tagged
    # PDFs (which already have selectable text) — the default mode refuses
    # those with "PriorOcrFound"/"Tagged PDF" and demands an override flag.
    # ocrmypdf.ocr's first two parameters are POSITIONAL (the first is named
    # ``input_file_or_options`` in current releases), so they must not be
    # passed as keywords.
    try:
        ocrmypdf.ocr(
            in_buf,
            out_buf,
            language="eng",
            output_type=out_type,
            skip_text=True,
            deskew=True,
            optimize=1,
            progress_bar=False,
        )
    except Exception as exc:
        raise RuntimeError(f"OCR failed: {exc}") from exc
    return out_buf.getvalue()


def render_braille_brf(rendered: RenderedPage, grade: int = 2, lang_table: str | None = None) -> bytes:
    """Convert the canonical HTML's text to BRF (Braille Ready File).

    Uses ``liblouis`` for translation. Default tables:
      grade 1 (uncontracted) -> ``en-us-g1.ctb``
      grade 2 (contracted)   -> ``en-us-g2.ctb``

    Output is suitable for refreshable Braille displays and Braille
    embossers. Raises RuntimeError with a friendly message if liblouis
    isn't installed.
    """
    import shutil
    import subprocess

    text = html_to_plain_text(rendered)
    if not text:
        raise RuntimeError("Nothing to braille-translate")

    lou = shutil.which("lou_translate")
    if not lou:
        raise RuntimeError(
            "lou_translate not found; install liblouis-bin in the Dockerfile"
        )

    table = lang_table or ("en-us-g2.ctb" if grade == 2 else "en-us-g1.ctb")
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
            f"liblouis translation failed: {exc.stderr.decode('utf-8', errors='replace')[:300]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("liblouis translation timed out") from exc
    translated = proc.stdout.decode("utf-8", errors="replace")

    # BRF expects ASCII-art Braille (40-char lines is convention).
    # liblouis returns a single string; wrap to 40 cols per Braille standard.
    lines: list[str] = []
    for para in translated.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            if len(cur) + len(word) + 1 <= 40:
                cur = (cur + " " + word).strip() if cur else word
            else:
                if cur:
                    lines.append(cur)
                cur = word if len(word) <= 40 else word[:40]
        if cur:
            lines.append(cur)
    return ("\n".join(lines) + "\n").encode("ascii", errors="replace")


READER_TEMPLATE = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>__RFTITLE__ — Reader</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
:root {{ --bg: #fdf6e3; --fg: #1d1d1d; --accent: #0a5fb5; --focus: #ffeb99; }}
body {{ background: var(--bg); color: var(--fg); font-family: Georgia, serif;
       max-width: 42rem; margin: 0 auto; padding: 4rem 1.5rem 8rem;
       line-height: 1.8; font-size: 1.15rem; }}
body.dyslexia {{ font-family: 'OpenDyslexic','Comic Sans MS',sans-serif; letter-spacing: 0.04em; }}
body.dark {{ --bg: #1d1d1d; --fg: #f8f9fa; }}
h1, h2, h3 {{ font-family: system-ui, sans-serif; color: var(--accent); }}
img {{ max-width: 100%; height: auto; }}
.line-focus p, .line-focus li {{ opacity: 0.45; transition: opacity 200ms; }}
.line-focus p.is-current, .line-focus li.is-current {{ opacity: 1; background: var(--focus); padding: 0 0.25rem; }}
.toolbar {{ position: fixed; top: 0; left: 0; right: 0; background: rgba(255,255,255,0.96);
            border-bottom: 1px solid #ccc; padding: 0.5rem 1rem; display: flex; gap: 0.4rem;
            align-items: center; flex-wrap: wrap; z-index: 1000; }}
.toolbar button, .toolbar select {{ background: #fff; border: 1px solid #ccc; padding: 0.35rem 0.7rem;
            border-radius: 4px; cursor: pointer; font: inherit; }}
.toolbar button:hover {{ background: #f0f0f0; }}
.toolbar button[aria-pressed=\"true\"] {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.toolbar .label {{ font-size: 0.8rem; color: #555; margin-right: 0.25rem; }}
.spacer {{ flex: 1; }}
body.dark .toolbar {{ background: rgba(40,40,40,0.96); color: #f8f9fa; }}
body.dark .toolbar button, body.dark .toolbar select {{ background: #2a2a2a; color: #f8f9fa; border-color: #555; }}
.dictionary-popup {{ position: absolute; background: #fff; border: 1px solid #888; border-radius: 6px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2); padding: 0.5rem 0.75rem; max-width: 22rem;
            font-size: 0.9rem; z-index: 2000; }}
</style>
</head>
<body>
<div class=\"toolbar\" role=\"toolbar\" aria-label=\"Reader controls\">
  <span class=\"label\">Read aloud</span>
  <button id=\"play\" aria-pressed=\"false\">▶ Play</button>
  <button id=\"stop\">■ Stop</button>
  <select id=\"voice\" aria-label=\"Voice\"></select>
  <select id=\"rate\" aria-label=\"Speed\">
    <option value=\"0.8\">0.8×</option><option value=\"1\" selected>1×</option>
    <option value=\"1.2\">1.2×</option><option value=\"1.5\">1.5×</option>
  </select>
  <span class=\"spacer\"></span>
  <span class=\"label\">Font</span>
  <button id=\"font-smaller\" aria-label=\"Smaller\">A−</button>
  <button id=\"font-larger\" aria-label=\"Larger\">A+</button>
  <button id=\"dyslexia\" aria-pressed=\"false\" title=\"Dyslexia-friendly font\">Dyslexia</button>
  <button id=\"dark\" aria-pressed=\"false\" title=\"Dark theme\">Dark</button>
  <button id=\"focus\" aria-pressed=\"false\" title=\"Highlight current line\">Focus</button>
</div>
<article id=\"content\">
<h1>__RFTITLE__</h1>
__RFBODY__
</article>
<script>
(function () {{
  var article = document.getElementById('content');
  var playBtn = document.getElementById('play');
  var stopBtn = document.getElementById('stop');
  var voiceSel = document.getElementById('voice');
  var rateSel = document.getElementById('rate');
  var smaller = document.getElementById('font-smaller');
  var larger = document.getElementById('font-larger');
  var dys = document.getElementById('dyslexia');
  var dark = document.getElementById('dark');
  var focusBtn = document.getElementById('focus');
  var synth = window.speechSynthesis;
  if (!synth) {{
    playBtn.disabled = true; stopBtn.disabled = true; voiceSel.disabled = true;
  }}

  // Voice picker
  function populateVoices() {{
    if (!synth) return;
    var voices = synth.getVoices() || [];
    voiceSel.innerHTML = '';
    voices.forEach(function (v, i) {{
      var opt = document.createElement('option');
      opt.value = String(i);
      opt.textContent = v.name + ' (' + v.lang + ')';
      if (v.default) opt.selected = true;
      voiceSel.appendChild(opt);
    }});
  }}
  populateVoices();
  if (synth) synth.onvoiceschanged = populateVoices;

  // Build paragraph list for sequential read + focus
  var paragraphs = Array.prototype.slice.call(article.querySelectorAll('p, li, h1, h2, h3, h4'));
  var idx = -1;
  var playing = false;

  function readNext() {{
    idx++;
    if (idx >= paragraphs.length) {{ stop(); return; }}
    var el = paragraphs[idx];
    paragraphs.forEach(function (p) {{ p.classList.remove('is-current'); }});
    el.classList.add('is-current');
    el.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
    var utter = new SpeechSynthesisUtterance(el.textContent.trim());
    var voices = synth.getVoices() || [];
    var sel = voices[parseInt(voiceSel.value)];
    if (sel) utter.voice = sel;
    utter.rate = parseFloat(rateSel.value);
    utter.onend = function () {{ if (playing) readNext(); }};
    synth.speak(utter);
  }}
  function play() {{ playing = true; playBtn.setAttribute('aria-pressed', 'true'); readNext(); }}
  function stop() {{ playing = false; idx = -1; playBtn.setAttribute('aria-pressed', 'false');
                     synth && synth.cancel(); paragraphs.forEach(function (p) {{ p.classList.remove('is-current'); }}); }}

  playBtn.addEventListener('click', function () {{ if (!playing) play(); else stop(); }});
  stopBtn.addEventListener('click', stop);

  // Font size
  var size = 1.15;
  function setSize(s) {{ size = Math.max(0.7, Math.min(2.5, s)); document.body.style.fontSize = size + 'rem'; }}
  smaller.addEventListener('click', function () {{ setSize(size - 0.1); }});
  larger.addEventListener('click', function () {{ setSize(size + 0.1); }});

  // Dyslexia font
  dys.addEventListener('click', function () {{
    var on = document.body.classList.toggle('dyslexia');
    dys.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Dark mode
  dark.addEventListener('click', function () {{
    var on = document.body.classList.toggle('dark');
    dark.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Line focus (uses .line-focus on body; CSS handles rest)
  focusBtn.addEventListener('click', function () {{
    var on = document.body.classList.toggle('line-focus');
    focusBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }});

  // Picture dictionary on click: highlight word, show definition popup via Free Dictionary API.
  article.addEventListener('click', function (e) {{
    var sel = window.getSelection();
    var word = sel.toString().trim();
    if (!word || word.indexOf(' ') >= 0) return;
    var existing = document.querySelector('.dictionary-popup');
    if (existing) existing.remove();
    var pop = document.createElement('div');
    pop.className = 'dictionary-popup';
    pop.style.left = (e.pageX + 10) + 'px';
    pop.style.top = (e.pageY + 10) + 'px';
    pop.textContent = 'Looking up "' + word + '"…';
    document.body.appendChild(pop);
    fetch('https://api.dictionaryapi.dev/api/v2/entries/en/' + encodeURIComponent(word))
      .then(function (r) {{ return r.json(); }})
      .then(function (j) {{
        if (Array.isArray(j) && j[0] && j[0].meanings && j[0].meanings[0]) {{
          var m = j[0].meanings[0];
          var def = (m.definitions && m.definitions[0] && m.definitions[0].definition) || '(no definition)';
          pop.innerHTML = '<strong>' + word + '</strong> <em>(' + (m.partOfSpeech || '') + ')</em><br>' + def;
        }} else {{
          pop.textContent = 'No definition found for "' + word + '".';
        }}
      }})
      .catch(function () {{ pop.textContent = 'Dictionary lookup failed.'; }});
    setTimeout(function () {{ pop.remove(); }}, 10000);
  }});
}})();
</script>
</body>
</html>
"""


def render_reader_html(rendered: RenderedPage) -> str:
    """Render an Immersive-Reader-style standalone HTML page.

    Uses the browser's built-in Web Speech API (no Azure required).
    Features: read-aloud with voice + speed picker, line focus, font
    size, dyslexia font, dark mode, click-a-word picture dictionary.
    Works in any modern browser, free, no API costs.
    """
    # READER_TEMPLATE doubles all its CSS/JS braces ({{ }}) so it can pass
    # through str.format(). But the document body routinely contains literal
    # braces (LaTeX math like \frac{a}{b}, set notation, code, JSON) which
    # str.format() would try to interpret as replacement fields and crash on
    # ("Single '{' encountered" / KeyError) — 500-ing the reader. So title and
    # body are no-field sentinels injected via str.replace(); .format() with no
    # args is used only to un-double the template's own braces.
    page = READER_TEMPLATE.format()
    page = page.replace("__RFTITLE__", _escape(rendered.title or "Document"))
    page = page.replace("__RFBODY__", rendered.html or "")
    return page
