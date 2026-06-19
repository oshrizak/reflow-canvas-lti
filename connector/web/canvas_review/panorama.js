/*
 * Equalify Reflow — Panorama-style Canvas overlay.
 *
 * Surfaces:
 *   - Files index rows (data-testid="table-row")
 *   - Module items wrapping a File
 *   - Any anchor referencing /files/<id> (Pages, Discussions, Modules, etc.)
 *
 * Each PDF gets a circular score dial. Clicking the dial opens the
 * Alternative Formats Menu modal. Instructors also get an "Edit
 * Accessible HTML" button that opens an in-modal editor with live
 * preview; saved edits become the canonical source for every other
 * format (ePub, audio, plain text, math, translations).
 */
(function () {
  "use strict";

  var SCRIPT = document.currentScript || (function () {
    var all = document.querySelectorAll('script[src*="/lti/panorama.js"]');
    return all[all.length - 1] || null;
  })();
  var ORIGIN = (function () { if (!SCRIPT) return ""; try { return new URL(SCRIPT.src).origin; } catch (_) { return ""; } })();
  var INST = (function () { if (!SCRIPT) return ""; try { return new URL(SCRIPT.src).searchParams.get("inst") || ""; } catch (_) { return ""; } })();
  if (!ORIGIN || !INST) { console.warn("[reflow] panorama.js loaded without origin/inst; aborting"); return; }

  var STATE = { courseId: null, userRole: null, scoresByFilename: null, csrfToken: null, oauth: null };
  // File extensions Reflow can process. Mirrors the watcher's _CONVERTIBLE_EXTS
  // on the backend so the dial appears on the same set of files. Add new
  // formats here when the backend learns to handle them.
  var CONVERTIBLE_EXT_RE = /\.(pdf|docx|doc|pptx|ppt|html|htm|epub)$/i;
  var FILE_HREF_RE = /\/files\/(\d+)(?:[\/?#"']|$)/;
  // The accessible-version link inside a published Canvas Page points at the
  // tool endpoint ``/canvas/panorama/alt/<job_id>/<fmt>``. We decorate it with
  // the OUTPUT (WCAG) score — the "after" — distinct from the source-PDF dial.
  var ACCESSIBLE_HREF_RE = /\/canvas\/panorama\/alt\/([^\/?#]+)\//;
  // Sent on every backend fetch. The ngrok-skip header bypasses ngrok's
  // free-tier browser interstitial — without it, ngrok returns an HTML warning
  // page instead of our JSON/HTML and the overlay's fetches fail to parse
  // (dials disappear). Harmless on Cloudflare/other hosts (ignored), so it's
  // safe to always send.
  var BACKEND_HEADERS = { "ngrok-skip-browser-warning": "1" };
  // Colours tuned to meet WCAG 2.2 AA non-text contrast (3:1 vs row
   // background) and text contrast (4.5:1 vs white) for the percentage
   // label rendered inside the dial. ``amber`` darkened from #cc7a00
   // (~4.36:1, fails AA) to #8a5500 (~6.2:1).
  var COLORS = { "red": "#b00020", "amber": "#8a5500", "green": "#2e7d32",
                 "dark-green": "#155724", "unscanned": "#6b7280" };

  // Catalogue of formats the modal shows. live=true means the backend
  // is wired; the rest are visible-but-disabled placeholders.
  // ``download: true`` means the format card should auto-download to
  // the user's downloads folder without opening a new tab. That's the
  // right UX for binary / file artifacts (PDF, EPUB, MP3, BRF, plain
  // text, markdown). Formats that render as a webpage in the browser
  // (HTML preview, HTML-with-math, Translate output, Immersive Reader)
  // stay as ``target="_blank"`` so faculty actually see the rendered
  // page in a new tab.
  // Groups organise the 11 alt-formats into mental buckets so the
  // grid is scannable instead of a wall of squares. ``order`` matters
  // because the rendered sections follow this list top-to-bottom.
  var FORMAT_GROUPS = [
    { id: "read",      label: "Read"             },
    { id: "listen",    label: "Listen & translate" },
    { id: "document",  label: "Document formats" },
    { id: "original",  label: "Original"         }
  ];
  var FORMATS = [
    { id: "html",      group: "read",     label: "Accessible HTML",    icon: "🌐", color: "#0a5fb5", live: true },
    { id: "html-math", group: "read",     label: "HTML with math",     icon: "∑",  color: "#5b3da6", live: true },
    { id: "txt",       group: "read",     label: "Plain text",         icon: "🅣", color: "#2e7d32", live: true, download: true },
    { id: "markdown",  group: "read",     label: "Markdown",           icon: "📝", color: "#1d1d1d", live: true, download: true },
    { id: "audio",     group: "listen",   label: "Audio (MP3)",        icon: "🎧", color: "#2e7d32", live: true, download: true },
    { id: "translate", group: "listen",   label: "Translate…",         icon: "🌐", color: "#2e7d32", live: true, picker: "language" },
    { id: "immersive", group: "listen",   label: "Immersive Reader",   icon: "👁", color: "#5b3da6", live: true },
    { id: "ocr",       group: "document", label: "Searchable PDF",     icon: "🔎", color: "#cc7a00", live: true, download: true },
    { id: "epub",      group: "document", label: "ePub",               icon: "📚", color: "#0e7a8a", live: true, download: true },
    { id: "braille",   group: "document", label: "Braille (BRF)",      icon: "⠿", color: "#5b3da6", live: true, download: true },
    { id: "source",    group: "original", label: "Source File",        icon: "📄", color: "#0a5fb5", live: true }
  ];

  // Language options for the Translate dialog. Covers CSUEB's largest
  // non-English-speaking communities plus globally common languages.
  var TRANSLATE_LANGUAGES = [
    { code: "es", label: "Spanish (Español)" },
    { code: "zh", label: "Chinese (中文)" },
    { code: "vi", label: "Vietnamese (Tiếng Việt)" },
    { code: "tl", label: "Tagalog / Filipino" },
    { code: "ko", label: "Korean (한국어)" },
    { code: "ja", label: "Japanese (日本語)" },
    { code: "ar", label: "Arabic (العربية)" },
    { code: "hi", label: "Hindi (हिन्दी)" },
    { code: "fr", label: "French (Français)" },
    { code: "de", label: "German (Deutsch)" },
    { code: "pt", label: "Portuguese (Português)" },
    { code: "it", label: "Italian (Italiano)" },
    { code: "ru", label: "Russian (Русский)" },
    { code: "pl", label: "Polish (Polski)" },
    { code: "tr", label: "Turkish (Türkçe)" },
    { code: "fa", label: "Persian / Farsi (فارسی)" },
    { code: "ur", label: "Urdu (اردو)" },
    { code: "bn", label: "Bengali (বাংলা)" },
    { code: "id", label: "Indonesian (Bahasa Indonesia)" },
    { code: "th", label: "Thai (ไทย)" }
  ];

  function getEnv() {
    var env = window.ENV || {};
    var cid = (env.COURSE && env.COURSE.id) || env.COURSE_ID || env.course_id;
    if (!cid && typeof env.context_asset_string === "string" && env.context_asset_string.indexOf("course_") === 0) {
      cid = env.context_asset_string.slice("course_".length);
    }
    if (!cid) { var m = location.pathname.match(/\/courses\/(\d+)/); if (m) cid = m[1]; }
    STATE.courseId = cid ? String(cid) : null;
    var roles = env.current_user_roles || [];
    STATE.userRole = (roles.indexOf("teacher") >= 0 || roles.indexOf("ta") >= 0 || roles.indexOf("admin") >= 0)
      ? "Instructor" : "Student";
  }

  async function loadScoresByFilename() {
    if (!STATE.courseId) return {};
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/scored_files?course_id=" + encodeURIComponent(STATE.courseId),
        { credentials: "omit", headers: BACKEND_HEADERS }
      );
      if (!r.ok) return {};
      var data = await r.json();
      STATE.scoresByFilename = data.by_filename || {};
      return STATE.scoresByFilename;
    } catch (e) { console.warn("[reflow] scored_files failed", e); return {}; }
  }

  // Fetch the per-session CSRF token bound to the LTI session cookie.
  // Required header on every state-changing request (approve / reject /
  // edit / pii-decision). 401 means no session cookie -- fall back to
  // anonymous behavior; state-changing actions will simply fail.
  async function loadCsrfToken() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/csrf",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.csrfToken = null; return null; }
      var data = await r.json();
      STATE.csrfToken = data.csrf_token || null;
      return STATE.csrfToken;
    } catch (e) {
      console.warn("[reflow] csrf token fetch failed", e);
      STATE.csrfToken = null;
      return null;
    }
  }

  // Build the standard set of headers for any state-changing request.
  // ``Content-Type`` is included because every endpoint accepts JSON.
  function csrfHeaders() {
    var h = Object.assign({ "Content-Type": "application/json" }, BACKEND_HEADERS);
    if (STATE.csrfToken) h["X-CSRF-Token"] = STATE.csrfToken;
    return h;
  }

  // Consent / authorization status for the current LTI session.
  // The actual gate is the LTI launch flow — this is just so the modal
  // footer can show a "View authorization terms" reminder and surface a
  // "please authorize" prompt to instructors who never launched the tool.
  async function loadConsentStatus() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/consent/status",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.consent = { agreed: false, reason: "fetch_failed" }; return STATE.consent; }
      STATE.consent = await r.json();
      return STATE.consent;
    } catch (e) {
      console.warn("[reflow] consent status fetch failed", e);
      STATE.consent = { agreed: false, reason: "network_error" };
      return STATE.consent;
    }
  }

  // Canvas-OAuth authorization status for the current LTI session. This is
  // distinct from consent (terms agreement): it's whether the instructor has
  // granted Reflow permission to read their files and publish Pages in their
  // courses. Without it, the watcher/bridge can't act for this instructor,
  // so we surface an "Authorize Reflow" prompt.
  async function loadOAuthStatus() {
    try {
      var r = await fetch(
        ORIGIN + "/canvas/panorama/oauth_status",
        { credentials: "include", headers: BACKEND_HEADERS }
      );
      if (!r.ok) { STATE.oauth = { authorized: false, is_instructor: false, has_session: false }; return STATE.oauth; }
      STATE.oauth = await r.json();
      return STATE.oauth;
    } catch (e) {
      console.warn("[reflow] oauth status fetch failed", e);
      STATE.oauth = { authorized: false, is_instructor: false, has_session: false };
      return STATE.oauth;
    }
  }

  // Open the Canvas OAuth consent flow in a popup window (instead of a
  // full-window navigation that yanks the instructor out of their page).
  // The callback returns a self-closing page that postMessages us back; we
  // listen for it, refresh status, and re-decorate so the prompt disappears.
  function openAuthPopup() {
    var url = ORIGIN + "/canvas/oauth/authorize?popup=1&return_url=" +
              encodeURIComponent(ORIGIN + "/canvas/oauth/authorized");
    var w = 600, h = 720;
    var y = window.top.outerHeight ? Math.max(0, (window.top.outerHeight - h) / 2) : 80;
    var x = window.top.outerWidth ? Math.max(0, (window.top.outerWidth - w) / 2) : 80;
    var popup = window.open(
      url, "reflow_authorize",
      "width=" + w + ",height=" + h + ",left=" + x + ",top=" + y + ",menubar=no,toolbar=no"
    );
    if (!popup) {
      // Popup blocked — fall back to a new tab so the user isn't stuck.
      window.open(url, "_blank", "noopener");
    }
    return popup;
  }

  // One-time listener: when the popup reports success, re-check status and
  // re-scan so badges/prompts update without a page reload.
  function _installOAuthMessageListener() {
    if (_installOAuthMessageListener._done) return;
    _installOAuthMessageListener._done = true;
    window.addEventListener("message", function (ev) {
      var d = ev && ev.data;
      if (!d || d.type !== "reflow-oauth" || !d.ok) return;
      loadOAuthStatus().then(function () {
        // Refresh any open modal banner and re-decorate the page.
        var banner = document.getElementById("reflow-pn-auth-banner");
        if (banner && STATE.oauth && STATE.oauth.authorized) {
          banner.outerHTML = "";
        }
        try { scheduleScan(); } catch (_) {}
      });
    }, false);
  }

  function fmtConsentDate(epoch) {
    if (!epoch) return "";
    try {
      var d = new Date(epoch * 1000);
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch (_) { return ""; }
  }

  function cleanFilename(text) {
    if (!text) return "";
    // Canvas prefixes filenames in screen-reader spans by file type:
    // "PDF File <name>", "Doc File <name>", "Word File <name>", etc.
    // Strip any "<type> File " or plain "File " prefix so the cleaned
    // filename matches the API's by_filename keys exactly.
    return text.trim()
      .replace(/^(?:PDF|Word|Doc|Document|Excel|Spreadsheet|PowerPoint|Slide|Slides|Image|Audio|Video|Text|HTML|EPub)?\s*File\s+/i, "")
      .trim();
  }

  // Spans that exist only for screen readers — they typically carry the
  // filename text prefixed with "PDF File" or similar, so a naive scan
  // matches them and anchors the dial to the checkbox column instead of
  // the visible name cell. Detect both Instructure's Emotion class and
  // common ``sr-only`` variants.
  var _SR_ONLY_RE = /\b(screenReaderContent|sr-only|visually-?hidden)\b/;
  function _isScreenReaderOnly(el) {
    var cls = el.className;
    if (typeof cls !== "string") return false;
    return _SR_ONLY_RE.test(cls);
  }

  function findFileRows() {
    var rows = document.querySelectorAll('[data-testid="table-row"]');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (row.dataset.reflowDecorated) continue;
      // Prefer the dedicated name cell when Canvas marks one — that's where
      // the user-visible filename lives in the new Files UI. Falls back to
      // scanning the whole row for legacy / non-table contexts.
      var nameCell = row.querySelector('[data-testid="table-cell-name"]');
      var scanRoots = nameCell ? [nameCell, row] : [row];
      var filename = "";
      var filenameSpan = null;
      for (var r = 0; r < scanRoots.length && !filename; r++) {
        var spans = scanRoots[r].querySelectorAll("span");
        for (var j = 0; j < spans.length; j++) {
          // Skip non-leaf spans: Canvas wraps the file-icon SVG and the
          // filename text inside the SAME outer flex container, and the
          // SVG carries an aria-labelling ``<title>PDF File</title>``
          // element. Calling .textContent on the outer span concatenates
          // the title text and the filename text — producing strings like
          // ``"PDF File01 SPRITE Chimera Student Module.pdf"`` that match
          // the extension regex but don't match any scored_files key.
          // Only the innermost text-bearing span (no element children)
          // carries the bare filename we need.
          if (spans[j].children.length > 0) continue;
          if (_isScreenReaderOnly(spans[j])) continue;
          var t = cleanFilename(spans[j].textContent);
          if (CONVERTIBLE_EXT_RE.test(t)) {
            filename = t;
            filenameSpan = spans[j];
            break;
          }
        }
      }
      if (!filename) continue;
      out.push({ row: row, filename: filename, filenameSpan: filenameSpan, nameCell: nameCell });
    }
    return out;
  }

  function findModuleFiles() {
    var items = document.querySelectorAll(
      'li.context_module_item.type_file, li.context_module_item.File, li.context_module_item[class*="attachment"]'
    );
    var out = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.dataset.reflowDecorated) continue;
      var titleEl = item.querySelector('.ig-title, .item_name a, a.title');
      var filename = titleEl ? titleEl.textContent.trim() : item.textContent.trim().split(/\s{2,}/)[0];
      if (!filename || !CONVERTIBLE_EXT_RE.test(filename)) continue;
      out.push({ item: item, filename: filename, anchor: titleEl || item });
    }
    return out;
  }

  function findFileLinkAnchors() {
    // Anchor surfaces. The first two ``a[href*="/files/"]`` and
    // ``a.instructure_file_link`` already cover most cases; the rest
    // explicitly call out surfaces where Canvas's themes/templates use
    // distinct classes:
    //
    //   * ``a.instructure_scribd_file``    -- legacy file preview popup
    //   * ``a.file_preview_link``          -- newer file preview link
    //   * ``a.instructure_inline_media_comment[href*="/files/"]``
    //                                      -- inline media inside pages
    //   * ``.user_content a[href*="/files/"]``
    //                                      -- WYSIWYG page bodies
    //   * ``.assignment_description a[href*="/files/"]``
    //                                      -- assignment instructions
    //   * ``.discussion-section a[href*="/files/"]``
    //                                      -- discussion topics
    //   * ``.context_module_item a[href*="/files/"]``
    //                                      -- module item rows
    //   * ``.attachment a[href*="/files/"]``
    //                                      -- assignment attachments
    var selectors = ['a[href*="/files/"]',
                     'a.instructure_file_link',
                     'a.instructure_scribd_file',
                     'a.file_preview_link',
                     'a.instructure_inline_media_comment[href*="/files/"]',
                     '.user_content a[href*="/files/"]',
                     '.assignment_description a[href*="/files/"]',
                     '.discussion-section a[href*="/files/"]',
                     '.context_module_item a[href*="/files/"]',
                     '.attachment a[href*="/files/"]'];
    var seen = new Set();
    var out = [];
    selectors.forEach(function (sel) {
      var links = document.querySelectorAll(sel);
      for (var i = 0; i < links.length; i++) {
        var a = links[i];
        if (a.dataset.reflowDecorated || seen.has(a)) continue;
        var href = a.getAttribute("href") || "";
        var m = href.match(FILE_HREF_RE);
        if (!m) continue;
        if (/\/files\/folder\//.test(href)) continue;
        seen.add(a);
        out.push({ anchor: a, fileId: m[1] });
      }
    });

    // Iframe-embedded PDF previews. Canvas's WYSIWYG inserts iframes
    // pointing to ``/courses/.../files/<id>/preview`` for inline file
    // previews. These don't have an ``href`` like an anchor, so we
    // synthesize a placeholder anchor sibling that the rest of the
    // overlay can attach to.
    var iframeSelectors = [
      'iframe.preview_overlay',
      'iframe[src*="/files/"][src*="/preview"]',
      'iframe.preview_iframe',
    ];
    iframeSelectors.forEach(function (sel) {
      var frames = document.querySelectorAll(sel);
      for (var i = 0; i < frames.length; i++) {
        var f = frames[i];
        if (f.dataset.reflowDecorated) continue;
        var src = f.getAttribute("src") || "";
        var m = src.match(FILE_HREF_RE);
        if (!m) continue;
        // Synthesize an anchor next to the iframe so the existing
        // button-attachment code path works unchanged.
        var anchor = document.createElement("a");
        anchor.href = src;
        anchor.style.display = "none";
        anchor.dataset.reflowSynthesized = "1";
        f.parentNode.insertBefore(anchor, f);
        f.dataset.reflowDecorated = "1";
        seen.add(anchor);
        out.push({ anchor: anchor, fileId: m[1], iframeHost: f });
      }
    });

    return out;
  }

  async function fetchScoresByFileId(fileIds) {
    if (!STATE.courseId || !fileIds.length) return {};
    var url = ORIGIN + "/canvas/panorama/score?course_id=" + encodeURIComponent(STATE.courseId)
            + "&file_ids=" + fileIds.join(",");
    var r = await fetch(url, { credentials: "omit", headers: BACKEND_HEADERS });
    if (!r.ok) return {};
    var data = await r.json();
    return data.scores || {};
  }

  // Accessible-version links point at the tool endpoint
  // ``/canvas/panorama/alt/<job_id>/<fmt>``. Collect them so we can show the
  // OUTPUT (WCAG) score on the "Open the accessible version" link inside a
  // published Canvas Page — the empty dial the old file-link path produced.
  function findAccessibleLinks() {
    var links = document.querySelectorAll('a[href*="/canvas/panorama/alt/"]');
    var seen = new Set();
    var out = [];
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.dataset.reflowDecorated || seen.has(a)) continue;
      // Only decorate accessible links that live in Canvas's own content (the
      // published Page stub). Our own overlay UI — the formats modal, the
      // student CTA, the language picker, the editor — is full of
      // ``/canvas/panorama/alt/…`` links too (every format card is one), and
      // those must NOT get a dial. All overlay UI uses ``reflow-pn-`` classes,
      // so an anchor with such an ancestor (or class) is ours; skip it.
      if (a.closest('[class*="reflow-pn"]')) continue;
      var href = a.getAttribute("href") || "";
      var m = href.match(ACCESSIBLE_HREF_RE);
      if (!m) continue;
      seen.add(a);
      out.push({ anchor: a, jobId: decodeURIComponent(m[1]) });
    }
    return out;
  }

  async function fetchScoresByJobId(jobIds) {
    if (!jobIds.length) return {};
    var url = ORIGIN + "/canvas/panorama/score_by_job?job_ids=" + jobIds.map(encodeURIComponent).join(",");
    var r = await fetch(url, { credentials: "omit", headers: BACKEND_HEADERS });
    if (!r.ok) return {};
    var data = await r.json();
    return data.scores || {};
  }

  // Local mirror of the backend severity buckets, for the rare case a
  // payload carries a score without its severity.
  function severityFor(score) {
    if (typeof score !== "number") return "unscanned";
    if (score >= 90) return "dark-green";
    if (score >= 67) return "green";
    if (score >= 33) return "amber";
    return "red";
  }

  // Map a payload to what the row dial should show. The dial decorates the
  // ORIGINAL PDF in Canvas, so once a file has finished converting it shows
  // the *source* accessibility estimate (the "before") — that's the number
  // faculty want next to the original file. The accessible version's WCAG
  // score (the "after") lives in the modal's report header. Pre-publish and
  // failure states still take priority and render an action badge so the
  // call-to-action is never masked by a number.
  function dialView(payload) {
    var js = String(payload.job_status || "").toLowerCase();
    if (js === "awaiting_approval") {
      return { score: 0, label: "!", color: COLORS.amber, severity: "amber",
               title: "Privacy review needed — click to approve or deny",
               aria: "A privacy review is needed before this document can finish converting. Click to approve or deny." };
    }
    if (js === "awaiting_review") {
      return { score: 0, label: "!", color: COLORS.amber, severity: "amber",
               title: "Needs your review — click to approve",
               aria: "Needs your review. Click to approve so students can see accessible formats." };
    }
    if (js === "processing" || js === "pii_scanning" || js === "processing_queued") {
      return { score: 0, label: "…", color: COLORS.unscanned, severity: "unscanned",
               title: "Converting… check back shortly",
               aria: "Converting. Check back shortly." };
    }
    // page_failed: conversion succeeded but Canvas rejected the page write
    // (usually a missing Pages permission on the connected account). The
    // accessibility result IS available — we fall through to the score
    // display below so the dial reflects the real "before/after" numbers,
    // and the completed-path logic augments the tooltip with the
    // page-failure note so the action is still discoverable.
    if (js === "failed" || js === "denied" || js === "rejected") {
      return { score: 0, label: "!", color: COLORS.red, severity: "red",
               title: "Conversion needs attention — click for details",
               aria: "Conversion needs attention. Click for details." };
    }
    // Completed.
    var hasSource = typeof payload.source_score === "number";
    var hasOutput = payload.status === "scored" && typeof payload.score === "number";
    // Students never see the raw score (it isn't student-actionable, and a
    // low "before" estimate on the original PDF would be alarming). Once an
    // accessible version exists, give them a positive confirmation instead.
    if (STATE.userRole !== "Instructor") {
      if (hasSource || hasOutput || js === "published") {
        return { score: 0, label: "✓", color: COLORS.green, severity: "green",
                 title: "Accessible version available — open formats",
                 aria: "Accessible version available. Open Alternative Formats Menu." };
      }
    }
    // page_failed addendum used by both score branches below — actionable
    // note about the Canvas Page write without burying the actual score.
    var pageFailedNote = (js === "page_failed")
      ? " (Canvas Page not created — click for details)"
      : "";
    // Instructors: show two independent measurements. These are NOT a
    // before/after of the same thing — the source_score is veraPDF's
    // PDF/UA-1 validation of the original PDF (1000+ rules, fractional),
    // while score is our WCAG structural checks on the generated HTML
    // (a focused subset, penalty-based). Faculty asked us to stop
    // implying improvement when the two simply measure different things.
    if (hasSource) {
      var ss = payload.source_score;
      var ssev = payload.source_severity || severityFor(ss);
      var after = hasOutput
        ? (" · Generated HTML (WCAG): " + payload.score + "%")
        : "";
      return { score: ss, label: ss + "%", color: COLORS[ssev] || COLORS.red,
               severity: ssev,
               title: "Original PDF (PDF/UA): " + ss + "%" + after + pageFailedNote
                      + " — different measurements, not a before/after. Click to open."
                      ,
               aria: "Original PDF scores " + ss
                     + " percent on PDF slash UA dash 1 by veraPDF"
                     + (hasOutput ? ("; generated HTML scores " + payload.score
                                    + " percent on a WCAG structural check subset. "
                                    + "These measure different documents and are not directly comparable")
                                  : "")
                     + (js === "page_failed" ? "; Canvas Page could not be created" : "")
                     + ". Open Alternative Formats Menu." };
    }
    // No source estimate (older job without signals): fall back to the
    // output score, then to a generic "available" / "open" affordance.
    if (hasOutput) {
      var sc = payload.score;
      var sev = payload.severity || severityFor(sc);
      return { score: sc, label: sc + "%", color: COLORS[sev] || COLORS.red,
               severity: sev,
               title: "Accessibility " + sc + "%" + pageFailedNote + " — open Alternative Formats Menu",
               aria: "Accessibility " + sc + "%"
                     + (js === "page_failed" ? "; Canvas Page could not be created" : "")
                     + ". Open Alternative Formats Menu." };
    }
    if (js === "published") {
      return { score: 0, label: "✓", color: COLORS.green, severity: "green",
               title: "Accessible version available",
               aria: "Accessible version available. Open Alternative Formats Menu." };
    }
    return { score: 0, label: "—", color: COLORS.unscanned, severity: "unscanned",
             title: "Open Alternative Formats Menu",
             aria: "Open Alternative Formats Menu." };
  }

  // Dial for the ACCESSIBLE-VERSION link (the tool endpoint). This is the
  // generated output, so it shows the WCAG accessibility score — the "after"
  // — rather than the original PDF's source estimate. Never shows the source
  // estimate, and reads positively for both faculty and students.
  function accessibleDialView(payload) {
    var hasOutput = payload && payload.status === "scored" && typeof payload.score === "number";
    if (hasOutput) {
      var sc = payload.score;
      var sev = payload.severity || severityFor(sc);
      return { score: sc, label: sc + "%", color: COLORS[sev] || COLORS.green,
               severity: sev,
               title: "Accessible version — WCAG accessibility " + sc + "% (open Alternative Formats Menu)",
               aria: "Accessible version. WCAG accessibility " + sc + " percent. Open Alternative Formats Menu." };
    }
    var js = String((payload && payload.job_status) || "").toLowerCase();
    if (js === "processing" || js === "pii_scanning" || js === "processing_queued") {
      return { score: 0, label: "…", color: COLORS.unscanned, severity: "unscanned",
               title: "Converting… check back shortly",
               aria: "Converting. Check back shortly." };
    }
    if (js === "published" || js === "awaiting_review") {
      return { score: 0, label: "✓", color: COLORS.green, severity: "green",
               title: "Accessible version available",
               aria: "Accessible version available. Open Alternative Formats Menu." };
    }
    return { score: 0, label: "✓", color: COLORS.green, severity: "green",
             title: "Accessible version",
             aria: "Accessible version. Open Alternative Formats Menu." };
  }

  // Should we attach a dial for this file? Instructors see every state
  // (so they can act on drafts/failures); students only see files that
  // have something usable for them (a score or a published version).
  function shouldDecorate(payload) {
    if (!payload) return false;
    if (STATE.userRole === "Instructor") return true;
    var js = String(payload.job_status || "").toLowerCase();
    return payload.status === "scored" || js === "published";
  }

  // Typography helper shared between the row dial and the big modal dial.
  // For numeric percentages we split into a large number + a small
  // superscript "%" so the digits get the visual breathing room they need.
  // For single-character labels (—, …, !, ✓) a single centered glyph is
  // cleaner than padding it out with a percent sign that doesn't apply.
  // ``sizes`` is a ``{num, pct, single}`` triple so the same helper can
  // serve both the 36-px row dial and the 52-px modal dial.
  function _dialTextSvg(label, color, sizes) {
    var s = sizes || { num: 14, pct: 7, single: 16 };
    var fam = "system-ui,-apple-system,Segoe UI,sans-serif";
    var pctMatch = String(label).match(/^(\d+)%$/);
    if (pctMatch) {
      var num = pctMatch[1];
      // ``100`` is 3 chars so shave a point off the number font to keep
      // the kerning inside the ring; ``98`` is 2 chars and gets full size.
      var nfs = num.length >= 3 ? Math.max(s.num - 1, 10) : s.num;
      return (
        '<text x="18" y="21.5" text-anchor="middle" font-weight="700" font-family="' + fam + '" fill="' + color + '" letter-spacing="-0.4">' +
        '<tspan font-size="' + nfs + '">' + num + '</tspan>' +
        '<tspan font-size="' + s.pct + '" dy="-3" dx="0.5">%</tspan>' +
        '</text>'
      );
    }
    // Non-numeric labels: single glyph, slightly lower y to optically
    // center within the ring.
    return (
      '<text x="18" y="22.5" text-anchor="middle" font-size="' + s.single + '" font-weight="700" font-family="' + fam + '" fill="' + color + '">' + label + '</text>'
    );
  }

  function makeDial(payload, filename, opts) {
    var v = (opts && opts.accessible) ? accessibleDialView(payload) : dialView(payload);
    var score = v.score;
    var severity = v.severity;
    var color = v.color;
    var label = v.label;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reflow-pn-dial reflow-pn-sev-" + severity;
    btn.title = v.title;
    btn.setAttribute("aria-label", v.aria);
    // Slimmer stroke + lighter track + split number/percent typography
    // give the dial a polished, production-ready feel without changing
    // the size of the slot in the actions cell. The viewBox stays 36×36
    // (with r=15.9155 so 2πr ≈ 100, letting stroke-dasharray take a raw
    // percentage value).
    btn.innerHTML =
      '<svg viewBox="0 0 36 36" width="36" height="36" aria-hidden="true">' +
      '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="#eef0f3" stroke-width="2.6"/>' +
      '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="' + color + '" stroke-width="2.6"' +
      '          stroke-dasharray="' + score + ' ' + (100 - score) + '" stroke-dashoffset="25"' +
      '          stroke-linecap="round" transform="rotate(-90 18 18)"/>' +
      _dialTextSvg(label, color, { num: 14, pct: 7, single: 16 }) +
      '</svg>';
    btn.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      openFormatsModal(payload, filename);
    });
    return btn;
  }

  function makeWidgetWrap(payload, filename) {
    var wrap = document.createElement("span");
    wrap.className = "reflow-pn-wrap";
    wrap.appendChild(makeDial(payload, filename));
    return wrap;
  }

  function openFormatsModal(payload, filename) {
    var existing = document.getElementById("reflow-pn-modal");
    if (existing) existing.remove();
    var score = payload.status === "scored" ? (payload.score || 0) : null;
    var severity = payload.severity || "unscanned";
    var color = COLORS[severity] || COLORS.unscanned;

    var overlay = document.createElement("div");
    overlay.id = "reflow-pn-modal";
    overlay.className = "reflow-pn-modal";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    var modal = document.createElement("div");
    modal.className = "reflow-pn-modal-card";
    modal.innerHTML =
      '<header class="reflow-pn-modal-header">' +
      '  <div class="reflow-pn-modal-titles">' +
      '    <h2>Alternative Formats Menu</h2>' +
      '    <p>' + esc(filename || "Document") + (payload.edited ? ' <span class="reflow-pn-edited">edited</span>' : '') + '</p>' +
      '  </div>' +
      '  <button class="reflow-pn-modal-close" type="button" aria-label="Close">&times;</button>' +
      '</header>' +
      _renderReportHeader(payload, score, color) +
      _renderVerapdfViolations(payload) +
      _renderCanvasPageBanner(payload) +
      _renderApprovalBar(payload) +
      (STATE.userRole === "Instructor"
        ? '<section class="reflow-pn-modal-tools">' +
          (payload.job_id ? '  <button class="reflow-pn-edit-btn" type="button">✏️ Edit Accessible HTML</button>' : '') +
          '  <span class="reflow-pn-tools-hint">Fix tables, math, chemistry — edits become the source for all formats below. Use Save then Publish in the editor when it is ready for students.</span>' +
          '</section>'
        : '') +
      _renderFormatBody(payload) +
      _renderAuthBanner() +
      _renderConsentBanner() +
      '<footer class="reflow-pn-modal-footer">' +
      _renderConsentFooter() +
      '  <label class="reflow-pn-switch"><input type="checkbox" checked><span class="reflow-pn-switch-slider"></span></label>' +
      '</footer>';

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // Phase 13: WCAG 2.1 AA hygiene for the modal itself.
    // - Remember what was focused so we can restore on close
    // - Move focus to the first interactive control inside the modal
    // - Trap Tab/Shift+Tab inside the modal
    // - Close on Escape; close button is reachable via keyboard
    var previouslyFocused = document.activeElement;
    _wcagify_modal(overlay, modal, previouslyFocused);

    // Insert the report dial SVG (instructor-only — students don't get
    // a score readout because the score is faculty-actionable, not
    // student-actionable).
    var dialContainer = modal.querySelector(".reflow-pn-report-dial");
    if (dialContainer) {
      // Same look as the row dial, sized up. Sizes tuned so the modal's
      // 52×52 render gives the digits room to breathe at high DPI.
      var mLabel = (score === null) ? "—" : (score + "%");
      dialContainer.innerHTML =
        '<svg viewBox="0 0 36 36" width="52" height="52" aria-hidden="true">' +
        '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="#eef0f3" stroke-width="2.6"/>' +
        '  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="' + color + '" stroke-width="2.6"' +
        '          stroke-dasharray="' + (score || 0) + ' ' + (100 - (score || 0)) + '" stroke-dashoffset="25"' +
        '          stroke-linecap="round" transform="rotate(-90 18 18)"/>' +
        _dialTextSvg(mLabel, color, { num: 11, pct: 6, single: 13 }) +
        '</svg>';
    }

    // Student smart-default primary CTA: wire up the "Open accessible
    // version" button to the Accessible HTML route for this job.
    // Faculty-side modal doesn't render this element.
    var studentPrimary = modal.querySelector(".reflow-pn-student-primary");
    if (studentPrimary && payload.job_id) {
      var previewSuffix = (payload.job_status !== "published" && STATE.userRole === "Instructor") ? "?preview=1" : "";
      studentPrimary.href = ORIGIN + "/canvas/panorama/alt/" + encodeURIComponent(payload.job_id) + "/html" + previewSuffix;
    }

    // Populate formats grid — grouped by FORMAT_GROUPS so faculty can
    // scan by purpose ("read" vs "listen & translate" vs "document
    // formats" vs "original") instead of staring at a wall of 11
    // identical-looking squares.
    var grid = modal.querySelector(".reflow-pn-modal-grid");
    var availableLive = payload.available_formats || ["html", "txt", "markdown"];
    function _buildFmtCard(fmt) {
      var enabled = fmt.live && (fmt.id === "source"
                                  || availableLive.indexOf(fmt.id) >= 0
                                  || fmt.id === "translate"
                                  || fmt.id === "immersive"
                                  || fmt.id === "braille"
                                  || fmt.id === "ocr");
      var el = document.createElement(enabled ? "a" : "div");
      el.className = "reflow-pn-fmt-card" + (enabled ? "" : " is-disabled");
      if (enabled) {
        if (fmt.picker === "language") {
          el.href = "#";
          el.addEventListener("click", function (e) {
            e.preventDefault();
            openLanguagePicker(payload);
          });
        } else if (fmt.id === "source") {
          el.href = "#";
        } else {
          // If the job hasn't been approved yet, instructors can still preview.
          // Append ?preview=1 so the backend bypasses the published-only gate.
          var previewSuffix = (payload.job_status !== "published" && STATE.userRole === "Instructor") ? "?preview=1" : "";
          el.href = ORIGIN + "/canvas/panorama/alt/" + encodeURIComponent(payload.job_id || "") + "/" + fmt.id + previewSuffix;
          if (fmt.download) {
            // ``download`` attribute on a same-origin link tells the
            // browser to save the response without navigating. Leaving
            // it empty defers the filename to the server's
            // Content-Disposition header (set by the alt-format route).
            // No ``target="_blank"`` here — that would briefly open an
            // empty tab while the connector renders, which is what
            // faculty noticed and asked to fix.
            el.setAttribute("download", "");
          } else {
            el.target = "_blank"; el.rel = "noopener";
          }
        }
      } else {
        el.setAttribute("aria-disabled", "true");
        el.title = "Not yet enabled.";
      }
      el.innerHTML =
        '<span class="reflow-pn-fmt-card-icon" style="background:' + fmt.color + '14;color:' + fmt.color + '">' +
        fmt.icon + '</span>' +
        '<span class="reflow-pn-fmt-card-label">' + esc(fmt.label) + '</span>' +
        (enabled ? "" : '<span class="reflow-pn-fmt-card-soon">soon</span>');
      return el;
    }
    FORMAT_GROUPS.forEach(function (group) {
      var fmtsInGroup = FORMATS.filter(function (f) { return f.group === group.id; });
      if (!fmtsInGroup.length) return;
      var section = document.createElement("section");
      section.className = "reflow-pn-fmt-group";
      section.innerHTML =
        '<h3 class="reflow-pn-fmt-group-label">' + esc(group.label) + '</h3>' +
        '<div class="reflow-pn-fmt-group-grid"></div>';
      var groupGrid = section.querySelector(".reflow-pn-fmt-group-grid");
      fmtsInGroup.forEach(function (fmt) {
        groupGrid.appendChild(_buildFmtCard(fmt));
      });
      grid.appendChild(section);
    });

    function close() { overlay.remove(); }
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    modal.querySelector(".reflow-pn-modal-close").addEventListener("click", close);

    // "Authorize Reflow" button (instructors who haven't OAuth'd). Opens the
    // consent popup; the message listener refreshes status on success.
    var authBtn = modal.querySelector(".reflow-pn-auth-btn");
    if (authBtn) {
      authBtn.addEventListener("click", function (e) {
        e.preventDefault();
        _installOAuthMessageListener();
        openAuthPopup();
      });
    }

    // Re-check AI-API consent on open so the "Authorization required…"
    // banner disappears for instructors who already accepted — including the
    // case where they just accepted in the consent tab (opened via the
    // button) without reloading the page. Also flips the footer to the
    // "✓ Authorized" line.
    loadConsentStatus().then(function (c) {
      if (!c || !c.agreed) return;
      var cb = modal.querySelector("#reflow-pn-consent-banner");
      if (cb) cb.remove();
      var ft = modal.querySelector(".reflow-pn-modal-footer .reflow-pn-footer-text");
      if (ft) {
        var when = fmtConsentDate(c.agreed_at);
        ft.innerHTML = '<span class="reflow-pn-consent-ok" aria-hidden="true">✓</span> ' +
          'Authorized' + (when ? ' on ' + esc(when) : '') +
          ' · <a href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">View terms</a>';
      }
    }).catch(function () {});

    // Approve / Reject / Request-edits — re-render the modal after a transition
    // so the badge color and the button set reflect the new state.
    _wireApprovalActions(modal, payload, function () { close(); openFormatsModal(payload, filename); });

    var editBtn = modal.querySelector(".reflow-pn-edit-btn:not(.reflow-pn-convert-btn)");
    if (editBtn) {
      editBtn.addEventListener("click", function () {
        openHtmlEditor(payload, filename);
      });
    }

    // The "refresh accessible page" action now lives in the Edit HTML editor
    // toolbar (openHtmlEditor, data-act="refresh") instead of a separate
    // button here — it's a post-edit utility, not a primary modal action.

    document.addEventListener("keydown", function escK(e) {
      if (e.key === "Escape") {
        var m = document.getElementById("reflow-pn-modal");
        var ed = document.getElementById("reflow-pn-editor");
        if (ed) ed.remove();
        else if (m) m.remove();
        document.removeEventListener("keydown", escK);
      }
    });
  }

  // Faculty HTML editor with live preview. Loads canonical HTML from
  // /edit/{job_id}, lets faculty rewrite it, saves via PUT.
  async function openHtmlEditor(payload, filename) {
    var jobId = payload.job_id;
    if (!jobId) return;
    var existing = document.getElementById("reflow-pn-editor");
    if (existing) existing.remove();

    var overlay = document.createElement("div");
    overlay.id = "reflow-pn-editor";
    overlay.className = "reflow-pn-editor";
    overlay.innerHTML =
      '<div class="reflow-pn-editor-card">' +
      '  <header>' +
      '    <div class="reflow-pn-editor-titles">' +
      '      <h2>Edit Accessible HTML</h2>' +
      '      <p>' + esc(filename || "Document") + ' <span id="reflow-pn-editor-state"></span></p>' +
      '    </div>' +
      '    <button class="reflow-pn-editor-close" type="button" aria-label="Close">&times;</button>' +
      '  </header>' +
      '  <div class="reflow-pn-editor-toolbar" role="toolbar" aria-label="Formatting">' +
      '    <button type="button" data-cmd="bold" title="Bold (Ctrl+B)"><b>B</b></button>' +
      '    <button type="button" data-cmd="italic" title="Italic (Ctrl+I)"><i>I</i></button>' +
      '    <button type="button" data-block="H2" title="Heading">H2</button>' +
      '    <button type="button" data-block="H3" title="Subheading">H3</button>' +
      '    <button type="button" data-block="P" title="Normal text" aria-label="Normal text">¶</button>' +
      '    <button type="button" data-cmd="insertUnorderedList" title="Bullet list">• List</button>' +
      '    <button type="button" data-cmd="insertOrderedList" title="Numbered list">1. List</button>' +
      '    <button type="button" data-act="link" title="Add a link">🔗 Link</button>' +
      '    <span class="reflow-pn-editor-toolbar-sep"></span>' +
      '    <button type="button" data-act="images" class="reflow-pn-editor-toggle" title="Add or fix image descriptions (alt text)">🖼 Image descriptions</button>' +
      '    <button type="button" data-act="toggle-source" class="reflow-pn-editor-toggle" title="Switch between the visual editor and raw HTML">&lt;/&gt; HTML</button>' +
      '    <span class="reflow-pn-editor-spacer"></span>' +
      '    <button type="button" data-act="revert" class="reflow-pn-btn-secondary">Revert to auto</button>' +
      '    <button type="button" data-act="save" class="reflow-pn-btn-primary">Save</button>' +
      '    <button type="button" data-act="publish" class="reflow-pn-btn-publish" title="Save your edits and publish — makes this accessible version visible to students in the course">✓ Publish</button>' +
      '  </div>' +
      '  <div class="reflow-pn-editor-body">' +
      '    <div class="reflow-pn-editor-visual" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Document content — type and format directly" spellcheck="true"></div>' +
      '    <aside class="reflow-pn-editor-alt" hidden aria-label="Image descriptions"></aside>' +
      '    <div class="reflow-pn-editor-sourcewrap" hidden>' +
      '      <textarea class="reflow-pn-editor-source" placeholder="Loading…" spellcheck="false"></textarea>' +
      '      <iframe class="reflow-pn-editor-preview" title="Live preview"></iframe>' +
      '    </div>' +
      '  </div>' +
      '  <footer class="reflow-pn-editor-status"></footer>' +
      '</div>';
    document.body.appendChild(overlay);

    var card = overlay.querySelector(".reflow-pn-editor-card");
    var visual = card.querySelector(".reflow-pn-editor-visual");
    var altPanel = card.querySelector(".reflow-pn-editor-alt");
    var sourceWrap = card.querySelector(".reflow-pn-editor-sourcewrap");
    var textarea = card.querySelector(".reflow-pn-editor-source");
    var preview = card.querySelector(".reflow-pn-editor-preview");
    var status = card.querySelector(".reflow-pn-editor-status");
    var stateEl = card.querySelector("#reflow-pn-editor-state");
    var sourceMode = false;   // false = visual (contenteditable), true = raw HTML
    var altOpen = false;

    function setStatus(msg, isErr) {
      status.textContent = msg;
      status.className = "reflow-pn-editor-status" + (isErr ? " is-error" : "");
    }
    function close() { overlay.remove(); }
    card.querySelector(".reflow-pn-editor-close").addEventListener("click", close);

    // The HTML we persist. In visual mode it's the contenteditable body
    // (minus our transient bookkeeping attributes); in source mode the
    // textarea is authoritative.
    function getHtml() {
      if (sourceMode) return textarea.value;
      var clone = visual.cloneNode(true);
      clone.querySelectorAll("[data-reflow-img]").forEach(function (el) {
        el.removeAttribute("data-reflow-img");
      });
      return clone.innerHTML;
    }
    function setHtml(html) {
      visual.innerHTML = html || "";
      textarea.value = html || "";
    }

    function refreshPreview() {
      var doc = preview.contentDocument;
      if (!doc) return;
      doc.open();
      doc.write(
        '<!doctype html><html><head><meta charset="utf-8">' +
        '<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>' +
        '<style>body{font-family:Georgia,serif;max-width:42rem;margin:1.5rem auto;padding:0 1rem;line-height:1.6;}' +
        'img{max-width:100%;}table{border-collapse:collapse;width:100%;}th,td{border:1px solid #ccc;padding:0.4rem;}</style>' +
        '</head><body>' + textarea.value + '</body></html>'
      );
      doc.close();
    }

    // Switch between the visual editor and the raw-HTML pane (which keeps the
    // live MathJax preview for power users editing math/tables).
    function setSourceMode(on) {
      if (on === sourceMode) return;
      sourceMode = on;
      var btn = card.querySelector('[data-act="toggle-source"]');
      if (sourceMode) {
        textarea.value = getHtml();    // serialise current visual edits
        refreshPreview();
        visual.hidden = true; altPanel.hidden = true; sourceWrap.hidden = false;
        if (btn) { btn.classList.add("is-active"); btn.textContent = "✓ Visual editor"; }
      } else {
        visual.innerHTML = textarea.value;   // adopt raw edits back
        sourceWrap.hidden = true; visual.hidden = false;
        if (btn) { btn.classList.remove("is-active"); btn.innerHTML = "&lt;/&gt; HTML"; }
        if (altOpen) { buildAltPanel(); altPanel.hidden = false; }
      }
    }

    function toggleAlt() {
      // Alt editing is tied to the live visual DOM, so leave source mode first.
      if (sourceMode) setSourceMode(false);
      altOpen = !altOpen;
      var btn = card.querySelector('[data-act="images"]');
      if (altOpen) { buildAltPanel(); altPanel.hidden = false; if (btn) btn.classList.add("is-active"); }
      else { altPanel.hidden = true; if (btn) btn.classList.remove("is-active"); }
    }

    // Build the alt-text side panel from the images currently in the document.
    // Each row edits the matching <img>'s alt attribute live; empty alts are
    // flagged so faculty can see at a glance what still needs a description.
    function buildAltPanel() {
      var imgs = visual.querySelectorAll("img");
      if (!imgs.length) {
        altPanel.innerHTML = '<div class="reflow-pn-alt-head"><h3>Image descriptions</h3>' +
          '<p>This document has no images.</p></div>';
        return;
      }
      var parts = ['<div class="reflow-pn-alt-head"><h3>Image descriptions (alt text)</h3>' +
        '<p>Describe each image so screen-reader users get the same information. ' +
        'If an image is purely decorative, mark it decorative so screen readers skip it.</p></div>' +
        '<ul class="reflow-pn-alt-list">'];
      Array.prototype.forEach.call(imgs, function (img, i) {
        img.setAttribute("data-reflow-img", String(i));
        // Decorative is encoded as role="presentation" + empty alt — the
        // standard markup that tells assistive tech to ignore the image.
        var deco = img.getAttribute("role") === "presentation" ||
                   img.getAttribute("aria-hidden") === "true";
        var alt = img.getAttribute("alt") || "";
        var missing = !deco && !alt.trim();
        var src = img.getAttribute("src") || "";
        parts.push(
          '<li class="reflow-pn-alt-item' + (missing ? ' is-missing' : '') +
          (deco ? ' is-decorative' : '') + '">' +
          '<img class="reflow-pn-alt-thumb" src="' + esc(src) + '" alt="" loading="lazy" />' +
          '<div class="reflow-pn-alt-fields">' +
          '<label for="reflow-pn-alt-' + i + '">Image ' + (i + 1) +
          (missing ? ' <span class="reflow-pn-alt-flag">needs description</span>' : '') +
          (deco ? ' <span class="reflow-pn-alt-deco-tag">decorative</span>' : '') + '</label>' +
          '<textarea id="reflow-pn-alt-' + i + '" class="reflow-pn-alt-input" data-img="' + i +
          '" rows="2" placeholder="Describe this image…"' + (deco ? ' disabled' : '') + '>' +
          esc(alt) + '</textarea>' +
          '<label class="reflow-pn-alt-deco"><input type="checkbox" class="reflow-pn-alt-deco-box" data-img="' +
          i + '"' + (deco ? ' checked' : '') + '> This image is decorative (skip for screen readers)</label>' +
          '</div></li>'
        );
      });
      parts.push('</ul>');
      altPanel.innerHTML = parts.join("");

      function imgFor(idx) { return visual.querySelector('img[data-reflow-img="' + idx + '"]'); }

      altPanel.querySelectorAll(".reflow-pn-alt-input").forEach(function (inp) {
        inp.addEventListener("input", function () {
          var target = imgFor(inp.getAttribute("data-img"));
          if (!target) return;
          target.setAttribute("alt", inp.value);
          var li = inp.closest(".reflow-pn-alt-item");
          if (li && !li.classList.contains("is-decorative")) {
            li.classList.toggle("is-missing", !inp.value.trim());
            var flag = li.querySelector(".reflow-pn-alt-flag");
            if (!inp.value.trim() && !flag) {
              var lbl = li.querySelector("label[for]");
              if (lbl) lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-flag">needs description</span>');
            } else if (inp.value.trim() && flag) {
              flag.remove();
            }
          }
          setStatus("Unsaved changes — click Save or Publish.");
        });
      });

      altPanel.querySelectorAll(".reflow-pn-alt-deco-box").forEach(function (box) {
        box.addEventListener("change", function () {
          var target = imgFor(box.getAttribute("data-img"));
          var li = box.closest(".reflow-pn-alt-item");
          var inp = li ? li.querySelector(".reflow-pn-alt-input") : null;
          var lbl = li ? li.querySelector("label[for]") : null;
          if (!target || !li) return;
          if (box.checked) {
            // Mark decorative: empty alt + presentation role, skip warnings.
            target.setAttribute("alt", "");
            target.setAttribute("role", "presentation");
            if (inp) { inp.value = ""; inp.disabled = true; }
            li.classList.remove("is-missing");
            li.classList.add("is-decorative");
            var f = li.querySelector(".reflow-pn-alt-flag");
            if (f) f.remove();
            if (lbl && !li.querySelector(".reflow-pn-alt-deco-tag")) {
              lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-deco-tag">decorative</span>');
            }
          } else {
            // No longer decorative: it needs a real description again.
            target.removeAttribute("role");
            if (inp) { inp.disabled = false; inp.focus(); }
            li.classList.remove("is-decorative");
            var t = li.querySelector(".reflow-pn-alt-deco-tag");
            if (t) t.remove();
            li.classList.add("is-missing");
            if (lbl && !li.querySelector(".reflow-pn-alt-flag")) {
              lbl.insertAdjacentHTML("beforeend", ' <span class="reflow-pn-alt-flag">needs description</span>');
            }
          }
          setStatus("Unsaved changes — click Save or Publish.");
        });
      });
    }

    // Toolbar. data-cmd → execCommand inline; data-block → formatBlock;
    // data-act → editor actions.
    var debouncePreview = null;
    textarea.addEventListener("input", function () {
      clearTimeout(debouncePreview);
      debouncePreview = setTimeout(refreshPreview, 300);
    });
    visual.addEventListener("input", function () {
      setStatus("Unsaved changes — click Save or Publish.");
    });

    card.querySelector(".reflow-pn-editor-toolbar").addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var cmd = b.getAttribute("data-cmd");
      var block = b.getAttribute("data-block");
      var act = b.getAttribute("data-act");
      if (cmd) {
        visual.focus();
        try { document.execCommand(cmd, false, null); } catch (_) {}
        return;
      }
      if (block) {
        visual.focus();
        try { document.execCommand("formatBlock", false, block); } catch (_) {}
        return;
      }
      if (act === "link") {
        var url = window.prompt("Link address (URL):", "https://");
        if (url) { visual.focus(); try { document.execCommand("createLink", false, url); } catch (_) {} }
        return;
      }
      if (act === "images") return toggleAlt();
      if (act === "toggle-source") return setSourceMode(!sourceMode);
      if (act === "save") return save();
      if (act === "revert") return revert();
      if (act === "publish") return publishFromEditor();
    });

    async function save() {
      setStatus("Saving…");
      try {
        var r = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "PUT",
          credentials: "include",
          headers: csrfHeaders(),
          body: JSON.stringify({ html: getHtml() })
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        var data = await r.json();
        setStatus("Saved. Edited HTML is now the source of truth for every format.");
        stateEl.textContent = data.edited ? " (edited)" : "";
      } catch (e) { setStatus("Save failed: " + e.message, true); }
    }
    async function revert() {
      if (!confirm("Discard your edits and revert to the auto-generated HTML?")) return;
      setStatus("Reverting…");
      try {
        await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "DELETE", credentials: "include", headers: csrfHeaders()
        });
        await loadInitial();
        setStatus("Reverted.");
        stateEl.textContent = "";
      } catch (e) { setStatus("Revert failed: " + e.message, true); }
    }
    async function publishFromEditor() {
      setStatus("Saving and publishing…");
      try {
        // Save the current edits first, so the published version includes them.
        var sv = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId), {
          method: "PUT", credentials: "include", headers: csrfHeaders(),
          body: JSON.stringify({ html: getHtml() })
        });
        if (!sv.ok) throw new Error("save HTTP " + sv.status);
        var sd = await sv.json();
        stateEl.textContent = sd.edited ? " (edited)" : "";
        // Then publish — makes the accessible version visible to students and
        // publishes the Canvas Page.
        var pb = await fetch(ORIGIN + "/canvas/panorama/approve/" + encodeURIComponent(jobId), {
          method: "POST", credentials: "include", headers: csrfHeaders(),
          body: JSON.stringify({ comment: null })
        });
        if (!pb.ok) {
          var e = await pb.json().catch(function () { return {}; });
          var detail = e && e.detail;
          if (detail && typeof detail === "object") detail = detail.message || JSON.stringify(detail);
          throw new Error(detail || ("HTTP " + pb.status));
        }
        setStatus("Published — students can now see this accessible version.");
      } catch (e) { setStatus("Publish failed: " + (e && e.message ? e.message : e), true); }
    }

    async function loadInitial() {
      setStatus("Loading…");
      try {
        var r = await fetch(ORIGIN + "/canvas/panorama/edit/" + encodeURIComponent(jobId),
                            { credentials: "include", headers: BACKEND_HEADERS });
        var data = await r.json();
        setHtml(data.html || "");
        stateEl.textContent = data.edited ? " (edited)" : "";
        if (sourceMode) refreshPreview();
        if (altOpen) buildAltPanel();
        setStatus(data.edited ? "Loaded your saved edit." : "Loaded auto-generated HTML.");
      } catch (e) {
        setStatus("Could not load HTML: " + e.message, true);
      }
    }

    loadInitial();
  }

  function getSelection(ta) {
    return { start: ta.selectionStart, end: ta.selectionEnd, text: ta.value.slice(ta.selectionStart, ta.selectionEnd) };
  }
  function setSelection(ta, start, end, repl) {
    ta.value = ta.value.slice(0, start) + repl + ta.value.slice(end);
    ta.focus();
  }

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  function injectStyles() {
    if (document.getElementById("reflow-pn-styles")) return;
    var s = document.createElement("style");
    s.id = "reflow-pn-styles";
    s.textContent = [
      // Row dial
      ".reflow-pn-wrap{display:inline-flex;align-items:center;font-family:system-ui,-apple-system,Segoe UI,sans-serif;}",
      ".reflow-pn-wrap.reflow-pn-row{position:absolute;top:50%;right:0.75rem;transform:translateY(-50%);margin:0;z-index:5;pointer-events:none;}",
      ".reflow-pn-wrap.reflow-pn-row .reflow-pn-dial{pointer-events:auto;}",
      // Legacy Files page: dial anchored to the filename cell, floated to the
      // right edge of the cell so it sits in the cell's slack space without
      // pushing the next column.
      ".reflow-pn-wrap.reflow-pn-cell{position:absolute !important;top:50% !important;right:0.5rem !important;transform:translateY(-50%) !important;margin:0 !important;z-index:5 !important;pointer-events:none !important;display:inline-block !important;width:auto !important;}",
      ".reflow-pn-wrap.reflow-pn-cell .reflow-pn-dial{pointer-events:auto;}",
      // New Files UI: dial floats absolutely in the inter-column gutter
      // between Status and Actions so it doesn't displace the row's 3-dot
      // menu. Top:50% + translateY(-50%) keeps it on the same baseline as
      // every other icon in the row. Left is negative so the wrap escapes
      // the actions cell's left edge into the gap.
      ".reflow-pn-wrap.reflow-pn-actions{position:absolute !important;top:50% !important;left:0.5rem !important;transform:translate(-50%, -50%) !important;display:inline-flex !important;align-items:center !important;justify-content:center !important;margin:0 !important;z-index:5 !important;pointer-events:none !important;}",
      ".reflow-pn-wrap.reflow-pn-actions .reflow-pn-dial{pointer-events:auto;}",
      ".reflow-pn-dial{background:transparent;border:0;padding:0;margin:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;line-height:1;border-radius:50%;transition:transform 120ms,box-shadow 120ms;vertical-align:middle;}",
      ".reflow-pn-dial:hover{transform:scale(1.08);box-shadow:0 0 0 4px rgba(0,0,0,0.05);}",
      ".reflow-pn-dial:focus{outline:none;box-shadow:0 0 0 3px rgba(10,95,181,0.4);}",
      // Modal
      ".reflow-pn-modal{position:fixed;inset:0;background:rgba(15,20,30,0.55);display:flex;align-items:center;justify-content:center;z-index:99999;font:14px system-ui,sans-serif;color:#1d1d1d;}",
      ".reflow-pn-modal-card{background:#fff;width:min(44rem,94vw);max-height:90vh;border-radius:12px;box-shadow:0 24px 64px rgba(15,20,30,0.22);display:flex;flex-direction:column;overflow:hidden;}",
      ".reflow-pn-modal-header{display:flex;align-items:flex-start;justify-content:space-between;padding:1.25rem 1.5rem 0.75rem;border-bottom:1px solid #ececec;}",
      ".reflow-pn-modal-titles h2{margin:0;font-size:1.05rem;font-weight:700;}",
      ".reflow-pn-modal-titles p{margin:0.2rem 0 0;color:#666;font-size:0.85rem;}",
      ".reflow-pn-edited{display:inline-block;background:#fff4cf;color:#8a5a00;border-radius:999px;padding:0 0.45rem;font-size:0.7rem;font-weight:700;margin-left:0.3rem;}",
      ".reflow-pn-modal-close{background:none;border:0;font-size:1.5rem;cursor:pointer;color:#666;line-height:1;padding:0;margin-left:1rem;}",
      ".reflow-pn-modal-report{display:flex;align-items:center;justify-content:space-between;padding:0.85rem 1.5rem;border-bottom:1px solid #f3f3f3;background:#fafafa;}",
      ".reflow-pn-report-text{font-size:0.9rem;font-weight:600;display:flex;align-items:center;gap:0.5rem;}",
      ".reflow-pn-pill{display:inline-block;padding:0.15rem 0.6rem;border-radius:999px;color:#fff;font-size:0.8rem;font-weight:700;}",
      ".reflow-pn-pill-grey{background:#9aa0a6;}",
      ".reflow-pn-pill-ok{background:#2e7d32;}",
      // Before → after strip: original PDF estimate on the left, accessible
      // version's WCAG score on the right, with an arrow between.
      ".reflow-pn-beforeafter{display:inline-flex;align-items:center;gap:0.55rem;}",
      ".reflow-pn-ba-item{display:inline-flex;flex-direction:column;align-items:center;gap:0.2rem;}",
      ".reflow-pn-ba-item small{font-size:0.68rem;font-weight:600;color:#5b6573;text-transform:uppercase;letter-spacing:0.02em;}",
      ".reflow-pn-ba-arrow{font-size:1.1rem;color:#9aa0a6;font-weight:700;}",
      // Phase 8: Canvas-Page-primary banner. Prominent green CTA
      // shown above the alt-format grid when an approved Canvas
      // Page exists for the job.
      ".reflow-pn-canvas-page-banner{padding:0.85rem 1.25rem;background:#e8f5ec;border-bottom:1px solid #b4dabe;}",
      ".reflow-pn-canvas-page-link{display:flex;align-items:center;gap:0.85rem;padding:0.7rem 0.95rem;background:#fff;border:1px solid #2e7d32;border-radius:8px;color:#103a1a;text-decoration:none;transition:background 120ms;}",
      ".reflow-pn-canvas-page-link:hover{background:#dff2e4;}",
      ".reflow-pn-canvas-page-link:focus{outline:2px solid #2e7d32;outline-offset:2px;}",
      ".reflow-pn-canvas-page-icon{flex:0 0 auto;font-size:1.4rem;}",
      ".reflow-pn-canvas-page-text{display:flex;flex-direction:column;gap:0.2rem;}",
      ".reflow-pn-canvas-page-text strong{font-size:0.95rem;}",
      ".reflow-pn-canvas-page-text small{font-size:0.78rem;color:#33493b;}",
      // Student variant of the report strip: no score dial, positive
      // confirmation copy. Hint text reads as one column under the pill.
      ".reflow-pn-modal-report--student{flex-direction:column;align-items:flex-start;gap:0.25rem;padding:1rem 1.5rem;}",
      ".reflow-pn-report-hint{font-size:0.83rem;color:#5b6573;font-weight:400;}",
      // VeraPDF violations disclosure. Sits between the score header and
      // the Canvas Page banner; collapsed by default so the modal stays
      // scannable. Contrast tuned for WCAG 2.2 AA (4.5:1 vs surrounding
      // backgrounds for the body text, 3:1 for the rule clause chip).
      ".reflow-pn-vrules{border-bottom:1px solid #f3f3f3;background:#fff;}",
      ".reflow-pn-vrules-summary{cursor:pointer;padding:0.85rem 1.5rem;font-size:0.9rem;color:#1d1d1d;font-weight:600;list-style:revert;}",
      ".reflow-pn-vrules-summary:focus-visible{outline:2px solid #1565c0;outline-offset:2px;}",
      ".reflow-pn-vrules-hint{font-size:0.83rem;color:#4a5260;font-weight:400;margin:0;padding:0 1.5rem 0.75rem;line-height:1.45;}",
      ".reflow-pn-vrules-hint a{color:#0a52a5;text-decoration:underline;}",
      ".reflow-pn-vrule-list{list-style:none;margin:0;padding:0 1.5rem 1rem;display:flex;flex-direction:column;gap:0.6rem;}",
      ".reflow-pn-vrule{background:#f6f8fa;border:1px solid #d6dbe1;border-radius:6px;padding:0.6rem 0.85rem;}",
      ".reflow-pn-vrule-head{display:flex;align-items:center;justify-content:space-between;gap:0.75rem;margin-bottom:0.3rem;}",
      ".reflow-pn-vrule-clause{display:inline-block;background:#0a52a5;color:#fff;border-radius:4px;padding:0.1rem 0.45rem;font-size:0.74rem;font-weight:700;font-family:'SFMono-Regular',Menlo,Consolas,monospace;}",
      ".reflow-pn-vrule-count{font-size:0.78rem;color:#4a5260;font-weight:600;}",
      ".reflow-pn-vrule-desc{font-size:0.88rem;color:#1d1d1d;line-height:1.4;}",
      // Student smart-default CTA. Big primary card, then a discreet
      // ``More formats`` disclosure containing the full grid.
      ".reflow-pn-student-cta{padding:1rem 1.5rem 0.5rem;display:flex;flex-direction:column;gap:0.75rem;}",
      ".reflow-pn-student-primary{display:flex;align-items:center;gap:0.9rem;padding:0.95rem 1.1rem;border:1px solid #2e7d32;border-radius:10px;background:#f1f8f4;text-decoration:none;color:#103a1a;transition:background 120ms,box-shadow 120ms,transform 120ms;}",
      ".reflow-pn-student-primary:hover{background:#e6f3ea;box-shadow:0 2px 8px rgba(46,125,50,0.15);transform:translateY(-1px);}",
      ".reflow-pn-student-primary:focus{outline:2px solid #2e7d32;outline-offset:2px;}",
      ".reflow-pn-student-primary-icon{flex:0 0 auto;font-size:1.65rem;line-height:1;}",
      ".reflow-pn-student-primary-text{display:flex;flex-direction:column;gap:0.15rem;}",
      ".reflow-pn-student-primary-text strong{font-size:1rem;font-weight:700;}",
      ".reflow-pn-student-primary-text small{font-size:0.82rem;color:#33493b;font-weight:400;}",
      ".reflow-pn-more-formats{font-size:0.85rem;}",
      ".reflow-pn-more-formats > summary{cursor:pointer;color:#0a5fb5;padding:0.25rem 0.4rem;border-radius:4px;list-style:none;font-weight:500;display:inline-flex;align-items:center;gap:0.25rem;}",
      ".reflow-pn-more-formats > summary::after{content:'▾';font-size:0.7em;}",
      ".reflow-pn-more-formats[open] > summary::after{content:'▴';}",
      ".reflow-pn-more-formats > summary:hover{background:#eef4fc;}",
      ".reflow-pn-more-formats > summary::-webkit-details-marker{display:none;}",
      // Student ``More formats`` reveals the same grouped grid but at
      // tighter padding (they came in via the big primary CTA above).
      ".reflow-pn-more-formats .reflow-pn-modal-grid{padding:0.6rem 0 0.4rem;}",
      ".reflow-pn-more-formats .reflow-pn-fmt-group-label{font-size:0.62rem;color:#7c8593;}",
      ".reflow-pn-modal-tools{display:flex;align-items:center;gap:0.6rem;padding:0.7rem 1.5rem;background:#eef4fc;border-bottom:1px solid #cfdcec;font-size:0.85rem;}",
      ".reflow-pn-edit-btn{background:#0a5fb5;color:#fff;border:0;padding:0.45rem 0.85rem;border-radius:6px;cursor:pointer;font-weight:600;}",
      ".reflow-pn-edit-btn:hover{background:#084a91;}",
      ".reflow-pn-tools-hint{color:#444;}",
      // Grid is now a stack of grouped sections, each with its own
      // auto-fit inner grid. ``minmax(11rem, 1fr)`` collapses to one
      // column on narrow viewports and expands to two or three on
      // wider modal widths without media-query plumbing.
      ".reflow-pn-modal-grid{display:flex;flex-direction:column;gap:0.9rem;padding:1rem 1.5rem 1.1rem;overflow:auto;}",
      ".reflow-pn-fmt-group{display:flex;flex-direction:column;gap:0.5rem;}",
      ".reflow-pn-fmt-group-label{margin:0;font-size:0.68rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#6b7280;}",
      ".reflow-pn-fmt-group-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:0.5rem;}",
      // Card refinement: slightly softer border, squircle icon
      // (border-radius 8px not 50%), refined hover (border shifts +
      // subtle lift). Icon background opacity tuned down from 20 to
      // 14 so the accent reads as accent, not a hard chip.
      ".reflow-pn-fmt-card{display:flex;align-items:center;gap:0.7rem;padding:0.55rem 0.8rem;border:1px solid #e7e9ec;border-radius:8px;text-decoration:none;color:#1d1d1d;background:#fff;transition:border-color 120ms,transform 120ms,box-shadow 120ms;}",
      ".reflow-pn-fmt-card:hover{border-color:#bfc4cb;transform:translateY(-1px);box-shadow:0 4px 12px rgba(15,20,30,0.07);}",
      ".reflow-pn-fmt-card:focus-visible{outline:2px solid #0a5fb5;outline-offset:2px;border-color:#0a5fb5;}",
      ".reflow-pn-fmt-card.is-disabled{opacity:0.45;cursor:not-allowed;pointer-events:none;}",
      ".reflow-pn-fmt-card-icon{flex:0 0 auto;width:1.9rem;height:1.9rem;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:0.95rem;line-height:1;}",
      ".reflow-pn-fmt-card-label{flex:1;font-size:0.88rem;font-weight:500;line-height:1.25;}",
      ".reflow-pn-fmt-card-soon{font-size:0.62rem;font-weight:700;text-transform:uppercase;color:#999;background:#f0f0f0;padding:0.12rem 0.4rem;border-radius:999px;}",
      // WCAG publication gate panel (only shown when the backend returns
      // a 409 with structured findings). Inline inside the approval bar
      // so faculty stays in flow.
      ".reflow-pn-wcag-gate{width:100%;padding:0.85rem 0 0;margin-top:0.6rem;border-top:1px solid #e7e9ec;display:flex;flex-direction:column;gap:0.6rem;}",
      ".reflow-pn-wcag-gate-title{margin:0;font-size:0.95rem;font-weight:700;color:#1d1d1d;}",
      ".reflow-pn-wcag-gate-subtitle{margin:0.25rem 0 0;font-size:0.85rem;font-weight:700;color:#1d1d1d;}",
      ".reflow-pn-wcag-gate-hint{margin:0;color:#5b6573;font-size:0.82rem;line-height:1.45;}",
      ".reflow-pn-wcag-checklist,.reflow-pn-wcag-findings{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:0.35rem;}",
      ".reflow-pn-wcag-checklist label,.reflow-pn-wcag-finding label{display:flex;align-items:flex-start;gap:0.55rem;padding:0.45rem 0.6rem;border:1px solid #e7e9ec;border-radius:6px;background:#fafbfc;cursor:pointer;font-size:0.86rem;line-height:1.35;}",
      ".reflow-pn-wcag-checklist label:hover,.reflow-pn-wcag-finding label:hover{border-color:#bfc4cb;background:#fff;}",
      ".reflow-pn-wcag-checklist input,.reflow-pn-wcag-finding input{margin-top:0.15rem;flex-shrink:0;}",
      ".reflow-pn-wcag-rule{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.78rem;font-weight:700;color:#b91c1c;background:#fee2e2;padding:0.05rem 0.4rem;border-radius:4px;flex-shrink:0;}",
      ".reflow-pn-wcag-msg{color:#1d1d1d;flex:1;}",
      ".reflow-pn-wcag-gate-actions{display:flex;gap:0.5rem;margin-top:0.3rem;}",
      ".reflow-pn-wcag-confirm,.reflow-pn-wcag-cancel{padding:0.5rem 0.95rem;border-radius:6px;border:1px solid transparent;cursor:pointer;font-weight:600;font-size:0.86rem;}",
      ".reflow-pn-wcag-confirm{background:#15803d;color:#fff;}",
      ".reflow-pn-wcag-confirm:hover{background:#166534;}",
      ".reflow-pn-wcag-cancel{background:#fff;border-color:#cdd3da;color:#1d1d1d;}",
      ".reflow-pn-wcag-cancel:hover{background:#f3f4f6;}",
      ".reflow-pn-modal-footer{display:flex;align-items:center;justify-content:space-between;padding:0.7rem 1.5rem;border-top:1px solid #ececec;background:#fafafa;font-size:0.83rem;color:#444;}",
      ".reflow-pn-switch{position:relative;display:inline-block;width:2.4rem;height:1.35rem;}",
      ".reflow-pn-switch input{opacity:0;width:0;height:0;}",
      ".reflow-pn-switch-slider{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:999px;transition:background 160ms;}",
      ".reflow-pn-switch-slider::before{content:'';position:absolute;left:0.15rem;top:0.15rem;width:1.05rem;height:1.05rem;background:#fff;border-radius:50%;transition:transform 160ms;}",
      ".reflow-pn-switch input:checked + .reflow-pn-switch-slider{background:#2e7d32;}",
      ".reflow-pn-switch input:checked + .reflow-pn-switch-slider::before{transform:translateX(1.05rem);}",
      // Editor
      ".reflow-pn-editor{position:fixed;inset:0;background:rgba(15,20,30,0.6);display:flex;align-items:center;justify-content:center;z-index:100000;font:14px system-ui,sans-serif;}",
      ".reflow-pn-editor-card{background:#fff;width:min(72rem,95vw);height:min(86vh,52rem);border-radius:10px;display:flex;flex-direction:column;box-shadow:0 30px 80px rgba(0,0,0,0.3);overflow:hidden;}",
      ".reflow-pn-editor-card header{display:flex;align-items:flex-start;justify-content:space-between;padding:1rem 1.25rem 0.5rem;border-bottom:1px solid #ececec;flex-wrap:wrap;}",
      ".reflow-pn-editor-card header h2{margin:0;font-size:1.05rem;}",
      ".reflow-pn-editor-card header p{margin:0.15rem 0 0;color:#666;font-size:0.85rem;}",
      // Stack the title + filename on the left so the filename doesn't float
      // in the middle of the header (flex space-between with 3 loose children).
      ".reflow-pn-editor-titles{display:flex;flex-direction:column;gap:0.1rem;min-width:0;flex:1;}",
      ".reflow-pn-editor-close{background:none;border:0;font-size:1.5rem;cursor:pointer;color:#666;line-height:1;padding:0;margin-left:1rem;}",
      ".reflow-pn-editor-toolbar{display:flex;align-items:center;gap:0.35rem;padding:0.55rem 1.25rem;border-bottom:1px solid #ececec;background:#fafafa;}",
      ".reflow-pn-editor-toolbar button{background:#fff;border:1px solid #d6d8db;padding:0.35rem 0.6rem;border-radius:5px;cursor:pointer;font-weight:600;color:#1d1d1d;}",
      ".reflow-pn-editor-toolbar button:hover{background:#f0f0f0;}",
      ".reflow-pn-editor-toolbar-sep{flex:0 0 auto;width:1px;height:1.4rem;background:#d6d8db;margin:0 0.5rem;}",
      ".reflow-pn-editor-spacer{flex:1;}",
      ".reflow-pn-btn-primary{background:#0a5fb5 !important;color:#fff !important;border-color:#0a5fb5 !important;}",
      ".reflow-pn-btn-primary:hover{background:#084a91 !important;}",
      ".reflow-pn-btn-secondary{color:#a00 !important;}",
      ".reflow-pn-btn-publish{background:#2e7d32 !important;color:#fff !important;border-color:#2e7d32 !important;}",
      ".reflow-pn-btn-publish:hover{background:#256527 !important;}",
      ".reflow-pn-editor-toggle.is-active{background:#e3edf7 !important;border-color:#0a5fb5 !important;color:#0a5fb5 !important;}",
      // Body is a flex row: the visual editor flexes to fill, the alt panel is
      // a fixed right rail when open, and the source/preview pane (a 2-col
      // grid) replaces the visual editor when "HTML" is toggled.
      ".reflow-pn-editor-body{display:flex;flex:1;min-height:0;}",
      ".reflow-pn-editor-visual{flex:1;min-width:0;overflow:auto;padding:1.5rem 1.75rem;background:#fff;color:#1d1d1d;font-family:Georgia,'Times New Roman',serif;font-size:1rem;line-height:1.65;outline:none;}",
      ".reflow-pn-editor-visual:focus{box-shadow:inset 0 0 0 2px rgba(10,95,181,0.18);}",
      ".reflow-pn-editor-visual h1,.reflow-pn-editor-visual h2,.reflow-pn-editor-visual h3{font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.25;margin:1.1em 0 0.4em;}",
      ".reflow-pn-editor-visual p{margin:0 0 0.8em;}",
      ".reflow-pn-editor-visual img{max-width:100%;height:auto;}",
      ".reflow-pn-editor-visual table{border-collapse:collapse;width:100%;}",
      ".reflow-pn-editor-visual th,.reflow-pn-editor-visual td{border:1px solid #ccc;padding:0.4rem;}",
      // Flag images that still have no alt text right in the editing surface —
      // but not ones explicitly marked decorative (role=presentation).
      ".reflow-pn-editor-visual img:not([alt]):not([role='presentation']),.reflow-pn-editor-visual img[alt='']:not([role='presentation']){outline:2px dashed #cc7a00;outline-offset:2px;}",
      ".reflow-pn-editor-alt{flex:0 0 21rem;overflow:auto;border-left:1px solid #ececec;background:#fafafa;padding:1rem 1.1rem;}",
      ".reflow-pn-alt-head h3{margin:0 0 0.25rem;font-size:0.95rem;}",
      ".reflow-pn-alt-head p{margin:0 0 0.85rem;font-size:0.8rem;color:#5b6573;line-height:1.45;}",
      ".reflow-pn-alt-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:0.7rem;}",
      ".reflow-pn-alt-item{display:flex;gap:0.6rem;padding:0.55rem;border:1px solid #e6e6e6;border-radius:7px;background:#fff;}",
      ".reflow-pn-alt-item.is-missing{border-color:#cc7a00;background:#fff8ee;}",
      ".reflow-pn-alt-thumb{flex:0 0 3rem;width:3rem;height:3rem;object-fit:cover;border-radius:4px;background:#eee;border:1px solid #e0e0e0;}",
      ".reflow-pn-alt-fields{flex:1;min-width:0;display:flex;flex-direction:column;gap:0.3rem;}",
      ".reflow-pn-alt-fields label{font-size:0.78rem;font-weight:600;color:#1d1d1d;}",
      ".reflow-pn-alt-flag{color:#cc7a00;font-weight:700;text-transform:uppercase;font-size:0.66rem;letter-spacing:0.02em;}",
      ".reflow-pn-alt-input{width:100%;box-sizing:border-box;border:1px solid #d6d8db;border-radius:5px;padding:0.4rem;font:inherit;font-size:0.82rem;resize:vertical;}",
      ".reflow-pn-alt-input:focus{outline:none;border-color:#0a5fb5;box-shadow:0 0 0 2px rgba(10,95,181,0.18);}",
      ".reflow-pn-alt-input:disabled{background:#f0f0f0;color:#9aa0a6;cursor:not-allowed;}",
      ".reflow-pn-alt-item.is-decorative{border-color:#c9d3df;background:#f4f7fb;}",
      ".reflow-pn-alt-deco-tag{color:#5b6573;font-weight:700;text-transform:uppercase;font-size:0.66rem;letter-spacing:0.02em;}",
      ".reflow-pn-alt-deco{display:flex;align-items:center;gap:0.35rem;font-size:0.75rem;color:#5b6573;font-weight:500;cursor:pointer;}",
      ".reflow-pn-alt-deco input{margin:0;flex:0 0 auto;}",
      ".reflow-pn-editor-sourcewrap{flex:1;display:grid;grid-template-columns:1fr 1fr;min-height:0;}",
      // The class-level display rules above override the plain ``hidden``
      // attribute's display:none, which left all three panes visible at once.
      // This higher-specificity rule restores hiding for the toggled panes.
      ".reflow-pn-editor-visual[hidden],.reflow-pn-editor-alt[hidden],.reflow-pn-editor-sourcewrap[hidden]{display:none !important;}",
      ".reflow-pn-editor-source{box-sizing:border-box;width:100%;height:100%;border:0;padding:1rem 1.25rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.85rem;line-height:1.55;letter-spacing:0.01em;tab-size:2;white-space:pre-wrap;resize:none;background:#f8f9fa;color:#1d1d1d;outline:none;border-right:1px solid #ececec;}",
      ".reflow-pn-editor-preview{box-sizing:border-box;width:100%;height:100%;border:0;}",
      ".reflow-pn-editor-status{padding:0.55rem 1.25rem;border-top:1px solid #ececec;background:#fafafa;color:#444;font-size:0.83rem;}",
      ".reflow-pn-editor-status.is-error{color:#b00020;background:#fff0f0;}",
      // Language picker (openLanguagePicker). The overlay wrapper had no CSS,
      // so the picker rendered invisibly in normal flow behind the modal —
      // clicking Translate appeared to do nothing. These give it a centered,
      // on-top modal presentation matching the rest of the overlay.
      ".reflow-pn-lang-overlay{position:fixed;inset:0;background:rgba(15,20,30,0.55);display:flex;align-items:center;justify-content:center;z-index:100001;font:14px system-ui,sans-serif;color:#1d1d1d;}",
      ".reflow-pn-lang-card{width:min(36rem,92vw);max-height:80vh;overflow:auto;background:#fff;border-radius:10px;box-shadow:0 24px 64px rgba(0,0,0,0.3);display:flex;flex-direction:column;}",
      ".reflow-pn-lang-card header{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:1px solid #ececec;}",
      ".reflow-pn-lang-card header h2{margin:0;font-size:1.05rem;}",
      ".reflow-pn-lang-close{background:none;border:0;font-size:1.5rem;line-height:1;cursor:pointer;color:#666;padding:0;}",
      ".reflow-pn-lang-list{list-style:none;margin:0;padding:0.75rem;display:grid;grid-template-columns:1fr 1fr;gap:0.4rem;}",
      ".reflow-pn-lang-list a{display:block;padding:0.55rem 0.75rem;border:1px solid #e6e6e6;border-radius:6px;text-decoration:none;color:#1d1d1d;background:#fff;}",
      ".reflow-pn-lang-list a:hover{background:#f3f6fb;border-color:#a4c3df;}",
      ".reflow-pn-lang-search{padding:0.75rem 1.5rem 0;}",
      ".reflow-pn-lang-search input{width:100%;padding:0.55rem 0.75rem;border:1px solid #d6d8db;border-radius:6px;font:inherit;}",
      ".reflow-pn-lang-grid{display:grid;grid-template-columns:1fr 1fr;gap:0.4rem;padding:0.85rem 1.5rem;max-height:22rem;overflow:auto;}",
      ".reflow-pn-lang-btn{display:flex;align-items:center;gap:0.6rem;padding:0.5rem 0.75rem;border:1px solid #e6e6e6;border-radius:6px;background:#fff;cursor:pointer;text-align:left;font:inherit;color:#1d1d1d;}",
      ".reflow-pn-lang-btn:hover{background:#f3f6fb;border-color:#a4c3df;}",
      ".reflow-pn-lang-code{font-weight:700;color:#0a5fb5;font-size:0.75rem;letter-spacing:0.04em;background:#eef4fc;padding:0.1rem 0.4rem;border-radius:4px;}",
      ".reflow-pn-lang-label{font-size:0.88rem;}",
      ".reflow-pn-lang-custom{display:flex;align-items:center;gap:0.6rem;padding:0.75rem 1.5rem;border-top:1px solid #ececec;background:#fafafa;}",
      ".reflow-pn-lang-custom label{flex:1;font-size:0.85rem;color:#444;display:flex;flex-direction:column;gap:0.25rem;}",
      ".reflow-pn-lang-custom input{padding:0.45rem 0.65rem;border:1px solid #d6d8db;border-radius:5px;font:inherit;}",
      // Consent banner + footer styles
      // Approval gate bar — shown above the formats grid
      ".reflow-pn-approval-bar{display:flex;align-items:center;justify-content:space-between;gap:0.75rem;margin:0.25rem 1.5rem 0;padding:0.65rem 0.9rem;background:#f4f7fb;border:1px solid #d6dee8;border-radius:6px;font-size:0.85rem;}",
      ".reflow-pn-approval-left{display:flex;align-items:center;gap:0.7rem;flex:1;min-width:0;}",
      ".reflow-pn-status-badge{display:inline-block;color:#fff;font-weight:700;font-size:0.72rem;letter-spacing:0.05em;padding:0.25rem 0.55rem;border-radius:4px;}",
      ".reflow-pn-approval-msg{color:#1a2233;flex:1;}",
      ".reflow-pn-approval-actions{display:flex;gap:0.5rem;flex-shrink:0;}",
      // PII privacy-review bar: stack the explanation, reason box, and
      // Approve/Deny buttons so the textarea gets full width.
      ".reflow-pn-pii-bar{flex-direction:column;align-items:stretch;gap:0.6rem;}",
      ".reflow-pn-pii-controls{display:flex;flex-direction:column;gap:0.5rem;}",
      ".reflow-pn-pii-justification{width:100%;font:inherit;font-size:0.85rem;padding:0.45rem 0.55rem;border:1px solid #c7d0db;border-radius:5px;resize:vertical;box-sizing:border-box;}",
      ".reflow-pn-btn-primary{background:#2e7d32;color:#fff;border:none;padding:0.4rem 0.9rem;border-radius:5px;font-weight:600;cursor:pointer;font:inherit;}",
      ".reflow-pn-btn-primary:hover{background:#256527;}",
      ".reflow-pn-btn-primary:disabled{background:#9ec5a0;cursor:wait;}",
      ".reflow-pn-btn-secondary{background:#fff;color:#1a2233;border:1px solid #c8cfd9;padding:0.4rem 0.9rem;border-radius:5px;cursor:pointer;font:inherit;}",
      ".reflow-pn-btn-secondary:hover{background:#f4f7fb;border-color:#a4b0c1;}",
      ".reflow-pn-consent-banner{display:flex;align-items:center;gap:0.6rem;margin:0.25rem 1.5rem 0;padding:0.7rem 0.9rem;background:#fff4e5;border:1px solid #f0b878;border-radius:6px;font-size:0.85rem;color:#5d4014;}",
      ".reflow-pn-consent-icon{font-size:1.1rem;}",
      ".reflow-pn-consent-text{flex:1;}",
      ".reflow-pn-consent-btn{display:inline-block;padding:0.35rem 0.85rem;background:#1f4e79;color:#fff !important;text-decoration:none;border-radius:5px;font-weight:600;font-size:0.8rem;}",
      ".reflow-pn-consent-btn:hover{background:#163960;}",
      ".reflow-pn-footer-text{font-size:0.8rem;color:#5d6b85;}",
      ".reflow-pn-footer-text a{color:#1f4e79;text-decoration:underline;}",
      ".reflow-pn-consent-ok{color:#2e7d32;font-weight:700;}"
    ].join("\n");
    document.head.appendChild(s);
  }

  // Render the consent banner shown at the top of the formats modal. For
  // students (and for instructors who have already consented) this is
  // empty; for instructors who haven't consented (or whose consent is
  // stale due to a version bump) it surfaces a prompt to authorize.
  // Render the Canvas-OAuth authorization prompt. Shown only to instructors
  // who have an LTI session but have NOT yet authorized Reflow against the
  // Canvas API — the state in which the watcher/bridge silently can't act.
  // The button opens a popup (see openAuthPopup) rather than navigating the
  // whole window away. Returns "" for students, for already-authorized
  // instructors, and when status is still unknown (avoid false alarms).
  function _renderAuthBanner() {
    var o = STATE.oauth;
    if (!o || o.authorized) return "";
    if (!o.is_instructor && STATE.userRole !== "Instructor") return "";
    var msg = "Authorize Reflow to read your course files and publish accessible pages. " +
              "Until you do, new uploads won't be converted in your courses.";
    return '<section id="reflow-pn-auth-banner" class="reflow-pn-consent-banner" role="status">' +
           '  <span class="reflow-pn-consent-icon" aria-hidden="true">🔑</span>' +
           '  <span class="reflow-pn-consent-text">' + esc(msg) + '</span>' +
           '  <button class="reflow-pn-consent-btn reflow-pn-auth-btn" type="button">Authorize Reflow</button>' +
           '</section>';
  }

  function _renderConsentBanner() {
    var c = STATE.consent || { agreed: false, reason: "unknown" };
    if (c.agreed) return "";
    // Don't nag students — only instructors trigger paid processing.
    if (STATE.userRole !== "Instructor") return "";
    var msg = "Authorization required to process new documents through the AI provider.";
    if (c.reason === "version_changed") {
      msg = "The Reflow disclaimer has been updated. Please re-authorize to continue using the tool.";
    }
    return '<section id="reflow-pn-consent-banner" class="reflow-pn-consent-banner" role="status">' +
           '  <span class="reflow-pn-consent-icon" aria-hidden="true">⚠️</span>' +
           '  <span class="reflow-pn-consent-text">' + esc(msg) + '</span>' +
           '  <a class="reflow-pn-consent-btn" href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">Authorize Reflow</a>' +
           '</section>';
  }

  // Render the footer line — always shown so faculty always know what's
  // happening with their data. Tone is informational, not nagging.
  function _renderConsentFooter() {
    var c = STATE.consent || { agreed: false, reason: "unknown" };
    if (c.agreed) {
      var when = fmtConsentDate(c.agreed_at);
      return '<span class="reflow-pn-footer-text">' +
             '  <span class="reflow-pn-consent-ok" aria-hidden="true">✓</span> ' +
             '  Authorized' + (when ? ' on ' + esc(when) : '') +
             '  · <a href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">View terms</a>' +
             '</span>';
    }
    if (STATE.userRole === "Instructor") {
      return '<span class="reflow-pn-footer-text">' +
             '  Alternative formats are visible to students. ' +
             '  <a href="' + ORIGIN + '/canvas/consent" target="_blank" rel="noopener">View Reflow authorization terms</a>' +
             '</span>';
    }
    return '<span class="reflow-pn-footer-text">Alternative formats are visible to students.</span>';
  }

  // Render the approval gate strip inside the formats modal. Shows the
  // current status (DRAFT / APPROVED / REJECTED), and — for instructors —
  // the Approve / Reject / Request-Edits buttons.
  // Header strip shown above the format grid. Instructors see the
  // standard accessibility score (the data point that prompts them to
  // act on a low-scoring document). Students get a positive
  // confirmation that an accessible version exists, since the raw
  // score is not actionable from their side.
  function _renderVerapdfViolations(payload) {
    // Only render when an actual VeraPDF audit produced violations.
    // The heuristic source_score path doesn't surface anything here —
    // it has no per-rule data to list.
    if (STATE.userRole !== "Instructor") return "";
    if (payload.source_score_provenance !== "verapdf") return "";
    var violations = Array.isArray(payload.verapdf_violations) ? payload.verapdf_violations : [];
    if (!violations.length) return "";
    // Group identical rule_ids defensively in case the backend ever
    // duplicates them. Sort by occurrence_count descending so the most
    // pervasive issues read first.
    var sorted = violations.slice().sort(function (a, b) {
      return (b.occurrence_count || 0) - (a.occurrence_count || 0);
    });
    var items = sorted.map(function (v) {
      var occ = v.occurrence_count || 1;
      var clauseRef = v.clause
        ? '<code class="reflow-pn-vrule-clause">PDF/UA &sect;' + esc(String(v.clause)) + '</code>'
        : '';
      var occText = occ === 1 ? '1 instance' : (occ + ' instances');
      return '<li class="reflow-pn-vrule">' +
             '  <div class="reflow-pn-vrule-head">' + clauseRef +
             '    <span class="reflow-pn-vrule-count">' + esc(occText) + '</span>' +
             '  </div>' +
             '  <div class="reflow-pn-vrule-desc">' + esc(String(v.description || v.rule_id || "Rule violation")) + '</div>' +
             '</li>';
    }).join("");
    var summary = sorted.length === 1
      ? "1 PDF/UA rule failed on the original PDF"
      : (sorted.length + " PDF/UA rules failed on the original PDF");
    return '<details class="reflow-pn-vrules">' +
           '  <summary class="reflow-pn-vrules-summary">' +
           '    <span>Why this score? &middot; ' + esc(summary) + '</span>' +
           '  </summary>' +
           '  <p class="reflow-pn-vrules-hint">' +
           '    These rules come from <a href="https://verapdf.org" target="_blank" rel="noopener">veraPDF</a>, ' +
           '    the PDF Association\'s PDF/UA-1 validator. Each maps to a WCAG 2.x criterion. ' +
           '    Fixing them in the source PDF (with Acrobat, PAC 2024, or your authoring tool) ' +
           '    will improve the score on the next re-conversion.' +
           '  </p>' +
           '  <ol class="reflow-pn-vrule-list">' + items + '</ol>' +
           '</details>';
  }

  function _renderReportHeader(payload, score, color) {
    var isInstructor = STATE.userRole === "Instructor";
    if (isInstructor) {
      // Two independent measurements — NOT a before/after of the same
      // thing. Faculty correctly flagged the older labelling as
      // misleading when the right number was lower than the left. The
      // two scores use different tools (veraPDF vs. our WCAG subset),
      // different documents (PDF vs. HTML), and different formulas
      // (proportional vs. penalty-based) so direct comparison isn't
      // meaningful.
      var hasSource = typeof payload.source_score === "number";
      var srcSev = payload.source_severity || severityFor(payload.source_score);
      var srcColor = COLORS[srcSev] || COLORS.unscanned;
      var srcPill = hasSource
        ? '<span class="reflow-pn-pill" style="background:' + srcColor + '">' + payload.source_score + '%</span>'
        : '<span class="reflow-pn-pill reflow-pn-pill-grey">not audited</span>';
      var htmlPill = (score === null)
        ? '<span class="reflow-pn-pill reflow-pn-pill-grey">not yet scored</span>'
        : '<span class="reflow-pn-pill" style="background:' + color + '">' + score + '%</span>';
      return '<section class="reflow-pn-modal-report">' +
        '  <div class="reflow-pn-report-text">' +
        '    <span class="reflow-pn-report-label">Two accessibility measurements</span>' +
        '    <span class="reflow-pn-beforeafter">' +
        '      <span class="reflow-pn-ba-item"><small>Original PDF — PDF/UA-1 (veraPDF)</small>' + srcPill + '</span>' +
        '      <span class="reflow-pn-ba-arrow" aria-hidden="true">·</span>' +
        '      <span class="reflow-pn-ba-item"><small>Generated HTML — WCAG checks</small>' + htmlPill + '</span>' +
        '    </span>' +
        '    <span class="reflow-pn-report-hint reflow-pn-report-hint-inline">' +
        '      <strong>These are not directly comparable.</strong> ' +
        '      The left number is veraPDF’s PDF/UA-1 validation of the original PDF (fraction of ~1000 rules passing). ' +
        '      The right number is our WCAG structural check subset on the generated HTML ' +
        '      (penalty-based: one error costs 12 points). They measure two different documents with two different tools — ' +
        '      a number being lower on either side does NOT mean accessibility regressed.' +
        '    </span>' +
        '  </div>' +
        '  <div class="reflow-pn-report-dial"></div>' +
        '</section>';
    }
    // Student variant. We only render this modal at all when a job
    // exists and (per the approval gate) is in a state the student can
    // see — so the language can be confident.
    return '<section class="reflow-pn-modal-report reflow-pn-modal-report--student">' +
      '  <div class="reflow-pn-report-text">' +
      '    <span class="reflow-pn-pill reflow-pn-pill-ok">✓ Accessible version available</span>' +
      '  </div>' +
      '  <div class="reflow-pn-report-hint">Choose a format that works for you.</div>' +
      '</section>';
  }

  // Phase 8: when an approved Canvas Page exists, promote it to a
  // prominent "primary" link so faculty + students see it BEFORE the
  // alternate-format grid. The original PDF in Canvas's Files UI is
  // labeled secondary; this banner is the route to the
  // student-facing accessible version.
  // Resolve a job's Canvas Page reference into a full, openable URL.
  // Canvas's pages API returns ``url`` as a bare slug (e.g. "my-page"),
  // and older jobs stored exactly that. A bare slug rendered as an href
  // resolves relative to the current Canvas path -> ".../courses/50594/<slug>"
  // which 404s (the real path needs ".../pages/<slug>"). Accept either a
  // full URL (new jobs store html_url) or a slug and always produce a
  // correct absolute URL.
  function _canvasPageHref(payload) {
    var u = (payload.canvas_page_url || "").trim();
    if (!u) return "";
    if (/^https?:\/\//i.test(u)) return u;          // already a full URL
    var slug = u.replace(/^\/+/, "");
    if (slug.indexOf("pages/") === 0) slug = slug.slice(6);
    var course = encodeURIComponent(STATE.courseId || "");
    return location.origin + "/courses/" + course + "/pages/" + slug;
  }

  function _renderCanvasPageBanner(payload) {
    if (!payload || !payload.canvas_page_url) return "";
    var href = _canvasPageHref(payload);
    if (!href) return "";
    var isStudent = STATE.userRole !== "Instructor";
    var label = isStudent
      ? "📘 Open the accessible Canvas page (primary)"
      : "📘 Open accessible Canvas page (this is the primary student-facing artifact)";
    return '<section class="reflow-pn-canvas-page-banner" aria-label="Primary accessible version">' +
      '  <a class="reflow-pn-canvas-page-link" target="_blank" rel="noopener" href="' +
      esc(href) + '">' +
      '    <span class="reflow-pn-canvas-page-icon" aria-hidden="true">📘</span>' +
      '    <span class="reflow-pn-canvas-page-text">' +
      '      <strong>' + esc(label) + '</strong>' +
      '      <small>The original PDF below is kept as a source copy. The reviewed Canvas Page above is the accessible version intended for student use.</small>' +
      '    </span>' +
      '  </a>' +
      '</section>';
  }

  // The body of the modal — formats. Instructors get the full grid of
  // every available format (HTML, audio, EPUB, OCR, Braille, etc.).
  // Students get a smart-default: a single prominent "Open accessible
  // version" CTA that links directly to Accessible HTML, with a
  // discreet "More formats ▾" expander that reveals the same grid for
  // anyone who needs audio, EPUB, Braille, translation, etc.
  function _renderFormatBody(payload) {
    var isInstructor = STATE.userRole === "Instructor";
    if (isInstructor) {
      return '<section class="reflow-pn-modal-grid"></section>';
    }
    return '<section class="reflow-pn-student-cta">' +
      '  <a class="reflow-pn-student-primary" data-fmt="html" href="#" target="_blank" rel="noopener">' +
      '    <span class="reflow-pn-student-primary-icon" aria-hidden="true">📖</span>' +
      '    <span class="reflow-pn-student-primary-text">' +
      '      <strong>Open accessible version</strong>' +
      '      <small>Reader-friendly HTML with proper headings, alt text, and reading order.</small>' +
      '    </span>' +
      '  </a>' +
      '  <details class="reflow-pn-more-formats">' +
      '    <summary>More formats</summary>' +
      '    <div class="reflow-pn-modal-grid"></div>' +
      '  </details>' +
      '</section>';
  }

  function _renderApprovalBar(payload) {
    if (!payload.job_id) return "";
    var status = payload.job_status || "awaiting_review";
    var isInstructor = STATE.userRole === "Instructor";

    // Privacy (PII) review: Reflow paused the conversion pending a human
    // decision. Surface Approve/Deny right here so faculty can act in one
    // click instead of hunting through the formats.
    if (status === "awaiting_approval") {
      if (!isInstructor) {
        return '<section class="reflow-pn-approval-bar" data-status="awaiting_approval">' +
               '  <div class="reflow-pn-approval-left">' +
               '    <span class="reflow-pn-status-badge" style="background:' + COLORS.amber + '">PRIVACY REVIEW</span>' +
               '    <span class="reflow-pn-approval-msg">This document is awaiting a privacy review by your instructor.</span>' +
               '  </div>' +
               '</section>';
      }
      return '<section class="reflow-pn-approval-bar reflow-pn-pii-bar" data-status="awaiting_approval">' +
             '  <div class="reflow-pn-approval-left">' +
             '    <span class="reflow-pn-status-badge" style="background:' + COLORS.amber + '">PRIVACY REVIEW</span>' +
             '    <span class="reflow-pn-approval-msg">Reflow flagged possible personal or sensitive information in this document. Approve to finish generating accessible formats, or deny to discard it. Your decision is logged.</span>' +
             '  </div>' +
             '  <div class="reflow-pn-pii-controls">' +
             '    <textarea class="reflow-pn-pii-justification" rows="2" maxlength="1000" aria-label="Reason for your privacy decision (required)">Reviewed by faculty in Canvas.</textarea>' +
             '    <div class="reflow-pn-approval-actions">' +
             '      <button class="reflow-pn-btn-primary" type="button" data-action="pii_approve">✓ Approve &amp; continue</button>' +
             '      <button class="reflow-pn-btn-secondary" type="button" data-action="pii_deny">Deny</button>' +
             '    </div>' +
             '  </div>' +
             '</section>';
    }

    // Converting / scanning: tell the professor it's still in flight.
    if (status === "processing" || status === "pii_scanning" || status === "processing_queued") {
      return '<section class="reflow-pn-approval-bar" data-status="' + esc(status) + '">' +
             '  <div class="reflow-pn-approval-left">' +
             '    <span class="reflow-pn-status-badge" style="background:' + COLORS.unscanned + '">CONVERTING</span>' +
             '    <span class="reflow-pn-approval-msg">This file is still being processed. Check back shortly.</span>' +
             '  </div>' +
             '</section>';
    }
    // Failed: tell the professor *why*, and what they can do about it.
    if (status === "failed" || status === "denied") {
      var reason = payload.error ? esc(payload.error) : "Conversion did not complete.";
      var hint = isInstructor
        ? " Re-upload the file to retry. Very large PDFs may need splitting; scanned/image-only PDFs are auto-OCR'd on retry."
        : "";
      return '<section class="reflow-pn-approval-bar" data-status="' + esc(status) + '">' +
             '  <div class="reflow-pn-approval-left">' +
             '    <span class="reflow-pn-status-badge" style="background:' + COLORS.red + '">NEEDS ATTENTION</span>' +
             '    <span class="reflow-pn-approval-msg">' + reason + hint + '</span>' +
             '  </div>' +
             '</section>';
    }
    var label, color, msg;
    if (status === "published") {
      label = "APPROVED";
      color = "#2e7d32";
      msg = "This version is visible to students.";
    } else if (status === "rejected") {
      label = "REJECTED";
      color = "#b00020";
      msg = "This version is hidden from students.";
    } else {
      label = "DRAFT";
      color = "#cc7a00";
      msg = "Students cannot see alternative formats until you approve.";
    }
    var buttons = "";
    if (isInstructor) {
      if (status === "awaiting_review" || status === "rejected") {
        buttons = '<button class="reflow-pn-approve-btn reflow-pn-btn-primary" type="button" data-action="approve">✓ Approve &amp; publish</button>'
                + (status === "awaiting_review" ? '<button class="reflow-pn-reject-btn reflow-pn-btn-secondary" type="button" data-action="reject">Reject</button>' : '');
      } else if (status === "published") {
        buttons = '<button class="reflow-pn-reedit-btn reflow-pn-btn-secondary" type="button" data-action="request_edits">↺ Request edits</button>'
                + '<button class="reflow-pn-unpublish-btn reflow-pn-btn-secondary" type="button" data-action="unpublish" title="Hide this accessible version from students and unpublish its Canvas page">⊘ Unpublish</button>';
      }
    }
    return '<section class="reflow-pn-approval-bar" data-status="' + esc(status) + '">' +
           '  <div class="reflow-pn-approval-left">' +
           '    <span class="reflow-pn-status-badge" style="background:' + color + '">' + label + '</span>' +
           '    <span class="reflow-pn-approval-msg">' + esc(msg) + '</span>' +
           '  </div>' +
           '  <div class="reflow-pn-approval-actions">' + buttons + '</div>' +
           '</section>';
  }

  // Phase 13: apply focus management + keyboard handling so modals
  // meet WCAG 2.1 AA. Specifically:
  //  - 2.1.2 No Keyboard Trap (Escape always closes)
  //  - 2.4.3 Focus Order (Tab cycles through interactive elements
  //    inside the modal; Shift+Tab cycles backwards)
  //  - 2.4.7 Focus Visible (we don't suppress default focus rings)
  //  - 4.1.2 Name/Role/Value (modal has role+aria-modal+aria-label)
  function _wcagify_modal(overlay, modal, previouslyFocused) {
    if (modal.getAttribute("role") !== "dialog") {
      modal.setAttribute("role", "dialog");
    }
    modal.setAttribute("aria-modal", "true");
    if (!modal.getAttribute("aria-label") && !modal.getAttribute("aria-labelledby")) {
      modal.setAttribute("aria-label", "Alternative Formats Menu");
    }

    function focusableNodes() {
      return Array.prototype.slice.call(modal.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), '
        + 'textarea:not([disabled]), select:not([disabled]), '
        + '[tabindex]:not([tabindex="-1"])'
      ));
    }
    var nodes = focusableNodes();
    if (nodes.length) {
      try { nodes[0].focus(); } catch (_) { /* defensive */ }
    }

    function onKey(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        teardown();
        return;
      }
      if (e.key !== "Tab") return;
      var current = document.activeElement;
      var f = focusableNodes();
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && current === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && current === last) {
        e.preventDefault();
        first.focus();
      }
    }

    function teardown() {
      try { overlay.removeEventListener("keydown", onKey, true); } catch (_) {}
      try { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); } catch (_) {}
      // Restore focus to whatever the user was on before opening.
      try {
        if (previouslyFocused && previouslyFocused.focus) {
          previouslyFocused.focus();
        }
      } catch (_) { /* element may have been removed */ }
    }

    overlay.addEventListener("keydown", onKey, true);

    // Wire the close button + backdrop click into the same teardown.
    var closer = modal.querySelector(".reflow-pn-modal-close");
    if (closer) {
      closer.addEventListener("click", function (e) { e.preventDefault(); teardown(); });
    }
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) teardown();
    });
  }

  // Replace the approval bar contents with the success state for the
  // given action. Pulled out so the multi-pass WCAG-gate flow can call
  // it after the resubmit succeeds.
  function _showApprovalSuccess(bar, action) {
    var pubBadge, pubColor, pubMsg;
    if (action === "approve") {
      pubBadge = "APPROVED"; pubColor = "#2e7d32";
      pubMsg = "Published — this accessible version is now visible to students.";
    } else if (action === "reject") {
      pubBadge = "REJECTED"; pubColor = "#b00020";
      pubMsg = "Rejected — this version is hidden from students.";
    } else if (action === "unpublish") {
      pubBadge = "DRAFT"; pubColor = "#cc7a00";
      pubMsg = "Unpublished — hidden from students and removed from the Pages list. Re-publish anytime with Approve & publish.";
    } else {
      pubBadge = "DRAFT"; pubColor = "#cc7a00";
      pubMsg = "Pulled back to draft for more edits.";
    }
    var pubActions = bar.querySelector(".reflow-pn-approval-actions");
    if (pubActions) pubActions.remove();
    var pubGate = bar.querySelector(".reflow-pn-wcag-gate");
    if (pubGate) pubGate.remove();
    var pubLeft = bar.querySelector(".reflow-pn-approval-left");
    if (pubLeft) {
      pubLeft.innerHTML =
        '<span class="reflow-pn-status-badge" style="background:' + pubColor + '">' + pubBadge + '</span>' +
        '<span class="reflow-pn-approval-msg">' + esc(pubMsg) + '</span>';
    }
  }

  // Render the WCAG gate panel: a checklist of 4 manual review items
  // (always shown) + a list of automated WCAG error findings (each
  // waivable individually with a justification). Replaces the row of
  // approve/reject buttons with the panel. Calls ``onConfirm`` once
  // faculty has signed off and clicked Confirm and publish; the caller
  // resubmits the POST with ``gateState.waivers`` / ``gateState.checklist``
  // populated. The same gateState reference is mutated in place so a
  // multi-pass gate (e.g. unwaived findings AFTER unchecked items)
  // preserves the work the user already did.
  function _renderWcagGatePanel(bar, detail, gateState, onConfirm) {
    var actions = bar.querySelector(".reflow-pn-approval-actions");
    var existing = bar.querySelector(".reflow-pn-wcag-gate");
    if (existing) existing.remove();
    if (actions) actions.style.display = "none";

    var findings = (detail && detail.findings) || [];
    var checklistItems = [
      { id: "headings",      label: "Headings are correct (H1 down, no skipped levels)" },
      { id: "alt_text",      label: "Every figure has meaningful alt text" },
      { id: "tables",        label: "Tables have row + column headers where needed" },
      { id: "reading_order", label: "Reading order matches the visual order" }
    ];

    var html = '<div class="reflow-pn-wcag-gate" role="region" aria-label="Publication gate">' +
      '<h3 class="reflow-pn-wcag-gate-title">Before you publish — quick review</h3>' +
      '<p class="reflow-pn-wcag-gate-hint">Confirm you have visually reviewed these items. Anything an automated check found that you cannot fix in the editor can be waived with a reason.</p>' +
      '<ul class="reflow-pn-wcag-checklist">';
    checklistItems.forEach(function (item) {
      var checked = !!gateState.checklist[item.id];
      html += '<li><label><input type="checkbox" data-checklist="' + item.id + '"' +
              (checked ? " checked" : "") +
              '> ' + esc(item.label) + '</label></li>';
    });
    html += '</ul>';

    if (findings.length) {
      html += '<h4 class="reflow-pn-wcag-gate-subtitle">Automated WCAG checks found ' + findings.length + ' issue' + (findings.length === 1 ? "" : "s") + '</h4>' +
              '<p class="reflow-pn-wcag-gate-hint">Fix in the editor before publishing, OR check the box to waive (and tell us why — recorded for the audit trail).</p>' +
              '<ul class="reflow-pn-wcag-findings">';
      findings.forEach(function (f) {
        var alreadyWaived = gateState.waivers.indexOf(f.rule_id) >= 0;
        html += '<li class="reflow-pn-wcag-finding" data-rule="' + esc(f.rule_id) + '">' +
                '  <label>' +
                '    <input type="checkbox" data-waive="' + esc(f.rule_id) + '"' +
                       (alreadyWaived ? " checked" : "") + '>' +
                '    <span class="reflow-pn-wcag-rule">' + esc(f.rule_id) + '</span>' +
                '    <span class="reflow-pn-wcag-msg">' + esc(f.message || "") + '</span>' +
                '  </label>' +
                '</li>';
      });
      html += '</ul>';
    }

    html += '<div class="reflow-pn-wcag-gate-actions">' +
            '  <button class="reflow-pn-btn-primary reflow-pn-wcag-confirm" type="button">✓ Confirm and publish</button>' +
            '  <button class="reflow-pn-btn-secondary reflow-pn-wcag-cancel" type="button">Cancel</button>' +
            '</div>' +
            '</div>';

    bar.insertAdjacentHTML("beforeend", html);
    var panel = bar.querySelector(".reflow-pn-wcag-gate");

    // Wire the checkboxes -> mutate gateState in place.
    panel.querySelectorAll('input[data-checklist]').forEach(function (cb) {
      cb.addEventListener("change", function () {
        gateState.checklist[cb.dataset.checklist] = !!cb.checked;
      });
    });
    panel.querySelectorAll('input[data-waive]').forEach(function (cb) {
      cb.addEventListener("change", function () {
        var rid = cb.dataset.waive;
        var i = gateState.waivers.indexOf(rid);
        if (cb.checked && i < 0) gateState.waivers.push(rid);
        if (!cb.checked && i >= 0) gateState.waivers.splice(i, 1);
      });
    });

    panel.querySelector(".reflow-pn-wcag-confirm").addEventListener("click", function () {
      // Guard against accidentally publishing without completing the
      // checklist — same guard the server enforces, but client-side so
      // faculty doesn't see a 409 they could have avoided.
      var unchecked = checklistItems.filter(function (it) { return !gateState.checklist[it.id]; });
      if (unchecked.length) {
        window.alert("Please confirm every item in the checklist before publishing.");
        return;
      }
      onConfirm();
    });
    panel.querySelector(".reflow-pn-wcag-cancel").addEventListener("click", function () {
      panel.remove();
      if (actions) actions.style.display = "";
      // Re-enable the approve button so faculty can try again or pivot.
      var ab = actions && actions.querySelector('button[data-action="approve"]');
      if (ab) { ab.disabled = false; ab.textContent = "✓ Approve & publish"; }
    });
  }

  // Wire up click handlers on the approval buttons. Called from inside
  // openFormatsModal after the modal is in the DOM.
  function _wireApprovalActions(modal, payload, refreshCallback) {
    var bar = modal.querySelector(".reflow-pn-approval-bar");
    if (!bar) return;
    bar.addEventListener("click", async function (e) {
      var btn = e.target.closest("button[data-action]");
      if (!btn) return;
      e.preventDefault();
      var action = btn.dataset.action;

      // PII privacy decision — distinct endpoint + payload (decision +
      // justification) from the publish approve/reject flow below.
      if (action === "pii_approve" || action === "pii_deny") {
        var decision = action === "pii_approve" ? "approved" : "denied";
        var ta = bar.querySelector(".reflow-pn-pii-justification");
        var justification = ((ta && ta.value) || "").trim();
        if (justification.length < 10) {
          if (ta) ta.focus();
          window.alert("Please enter a brief reason (at least 10 characters) for the privacy decision — it's recorded for the audit trail.");
          return;
        }
        if (decision === "denied" && !window.confirm(
              "Deny processing? Reflow will discard this document and generate no accessible formats.")) return;
        btn.disabled = true; btn.textContent = "Working…";
        try {
          var pr = await fetch(
            ORIGIN + "/canvas/panorama/pii-decision/" + encodeURIComponent(payload.job_id),
            {
              method: "POST",
              credentials: "include",
              headers: csrfHeaders(),
              body: JSON.stringify({ decision: decision, justification: justification })
            }
          );
          if (pr.status === 409) {
            // Not an error: the job already moved past the privacy gate (it
            // finished converting, or another tab/instructor acted). Show the
            // resolved state instead of a scary failure.
            var c409 = bar.querySelector(".reflow-pn-pii-controls");
            if (c409) c409.remove();
            var l409 = bar.querySelector(".reflow-pn-approval-left");
            if (l409) {
              l409.innerHTML =
                '<span class="reflow-pn-status-badge" style="background:#2e7d32">DONE</span>' +
                '<span class="reflow-pn-approval-msg">This document already cleared the privacy review and finished converting. Close and reopen this menu to see the latest.</span>';
            }
            return;
          }
          if (!pr.ok) {
            var pt = await pr.text();
            throw new Error("HTTP " + pr.status + ": " + pt.slice(0, 200));
          }
          // Give clear in-place feedback. The canvas status only flips off
          // 'awaiting_approval' on the next bridge tick, so reopening the modal
          // with the (stale) payload would look like nothing happened.
          var controls = bar.querySelector(".reflow-pn-pii-controls");
          if (controls) controls.remove();
          var left = bar.querySelector(".reflow-pn-approval-left");
          if (left) {
            left.innerHTML =
              '<span class="reflow-pn-status-badge" style="background:' +
              (decision === "approved" ? "#2e7d32" : "#b00020") + '">' +
              (decision === "approved" ? "APPROVED" : "DENIED") + '</span>' +
              '<span class="reflow-pn-approval-msg">' +
              (decision === "approved"
                ? "Approved. Reflow is finishing the conversion — the accessible page will appear in a minute or two."
                : "Denied. Reflow will discard this document; no accessible formats will be generated.") +
              '</span>';
          }
        } catch (err) {
          btn.disabled = false;
          btn.textContent = decision === "approved" ? "✓ Approve & continue" : "Deny";
          window.alert("Privacy decision failed: " + err.message);
        }
        return;
      }

      var endpoint = { approve: "approve", reject: "reject", request_edits: "request-edits", unpublish: "unpublish" }[action];
      if (!endpoint) return;
      var prompt = action === "reject" ? "Reject this accessible version? Students will not see it."
                 : action === "request_edits" ? "Pull this back to draft for more edits?"
                 : action === "unpublish" ? "Unpublish this accessible version? Students will no longer see it and its Canvas page will be unpublished. You can re-publish it anytime."
                 : null;
      if (prompt && !window.confirm(prompt)) return;
      btn.disabled = true; btn.textContent = "Working…";

      // Persist gate state across the (potentially) multi-pass approve
      // flow. The server runs WCAG + checklist gates; we may need to
      // re-POST with waivers + checklist filled in. ``reject``,
      // ``request_edits`` and ``unpublish`` ignore both fields.
      var gateState = {
        waivers: [],
        checklist: { headings: false, alt_text: false, tables: false, reading_order: false }
      };

      async function _sendApprovalRequest() {
        return await fetch(
          ORIGIN + "/canvas/panorama/" + endpoint + "/" + encodeURIComponent(payload.job_id),
          {
            method: "POST",
            credentials: "include",
            headers: csrfHeaders(),
            body: JSON.stringify({
              comment: null,
              waivers: gateState.waivers,
              checklist: gateState.checklist
            })
          }
        );
      }

      try {
        var r = await _sendApprovalRequest();

        // Gate handling: when REQUIRE_WCAG_GATE=true the backend returns
        // structured 409s for the two gate failures (unwaived WCAG errors
        // or an incomplete reviewer checklist). Swap the approval bar
        // for an inline gate panel so faculty can act on the findings
        // and resubmit without leaving the modal.
        if (r.status === 409 && action === "approve") {
          var gatePayload = await r.json().catch(function () { return {}; });
          var detail = gatePayload && gatePayload.detail;
          if (detail && typeof detail === "object" &&
              (detail.error === "wcag_gate_blocked" || detail.error === "checklist_incomplete")) {
            _renderWcagGatePanel(bar, detail, gateState, async function () {
              // Resubmit with the populated gate state. Same handler;
              // success path below runs through the normal pubBadge code.
              var rr = await _sendApprovalRequest();
              if (rr.status === 409) {
                // Still gated (e.g. faculty waived some findings but
                // missed the checklist). Re-render with the new detail.
                var d2 = await rr.json().catch(function () { return {}; });
                _renderWcagGatePanel(bar, d2.detail || {}, gateState, arguments.callee);
                return;
              }
              if (!rr.ok) {
                var t2 = await rr.text();
                window.alert("Action failed: HTTP " + rr.status + ": " + t2.slice(0, 200));
                return;
              }
              _showApprovalSuccess(bar, "approve");
            });
            return;
          }
        }

        if (!r.ok) {
          var txt = await r.text();
          throw new Error("HTTP " + r.status + ": " + txt.slice(0, 200));
        }
        _showApprovalSuccess(bar, action);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = action === "approve" ? "✓ Approve & publish"
                        : action === "reject"  ? "✕ Reject"
                        : action === "unpublish" ? "⊘ Unpublish"
                                               : "↺ Request edits";
        window.alert("Action failed: " + (err && err.message ? err.message : err));
      }
    });
  }

  // Language picker for the Translate format card. Presents a small
  // overlay with common languages; selecting one navigates to
  // ``/canvas/panorama/alt/{job_id}/translate?lang=<code>``.
  function openLanguagePicker(payload) {
    // Full language list (defined once at the top of the bundle). The AI
    // translator also accepts any language name, so a free-text field below
    // covers everything not in the list.
    var langs = TRANSLATE_LANGUAGES;
    function tUrl(code) {
      var preview = (payload.job_status !== "published" && STATE.userRole === "Instructor")
        ? "?preview=1" : "";
      // Backend matches the colon form ``/alt/{job}/translate:<lang>`` (see
      // ``fmt.startswith("translate:")``), not a ``?lang=`` query param.
      return ORIGIN + "/canvas/panorama/alt/" + encodeURIComponent(payload.job_id || "")
           + "/translate:" + encodeURIComponent(code) + preview;
    }
    var ov = document.createElement("div");
    ov.className = "reflow-pn-overlay reflow-pn-lang-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-label", "Choose a translation language");
    ov.innerHTML =
      '<div class="reflow-pn-lang-card">' +
      '  <header><h2>Translate to…</h2>' +
      '    <button class="reflow-pn-lang-close" type="button" aria-label="Close">&times;</button>' +
      '  </header>' +
      '  <ul class="reflow-pn-lang-list"></ul>' +
      '  <div class="reflow-pn-lang-custom">' +
      '    <label>Other language' +
      '      <input type="text" class="reflow-pn-lang-other" placeholder="e.g. Swahili, Hmong, Punjabi…" />' +
      '    </label>' +
      '    <button type="button" class="reflow-pn-lang-go reflow-pn-btn-primary">Translate</button>' +
      '  </div>' +
      '</div>';
    var ul = ov.querySelector(".reflow-pn-lang-list");
    langs.forEach(function (lang) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = tUrl(lang.code);
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = lang.label;
      li.appendChild(a);
      ul.appendChild(li);
    });
    // Free-text fallback for any language not listed.
    var other = ov.querySelector(".reflow-pn-lang-other");
    var go = ov.querySelector(".reflow-pn-lang-go");
    function goOther() {
      var v = (other && other.value || "").trim();
      if (!v) { if (other) other.focus(); return; }
      window.open(tUrl(v), "_blank", "noopener");
    }
    if (go) go.addEventListener("click", goOther);
    if (other) other.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); goOther(); }
    });
    ov.querySelector(".reflow-pn-lang-close").addEventListener("click", function () {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    });
    ov.addEventListener("click", function (e) {
      if (e.target === ov && ov.parentNode) ov.parentNode.removeChild(ov);
    });
    document.body.appendChild(ov);
  }

  // -------------------------------------------------------------------
  // Bootstrap
  // -------------------------------------------------------------------
  //
  // Canvas is a single-page app at runtime: clicking a course nav swaps
  // the main panel without a full reload. We scan once at
  // DOMContentLoaded and again on every coalesced DOM mutation.

  async function decorateAllPdfs() {
    if (!STATE.courseId) return;

    // Three Canvas surfaces, three lookup keys.
    // - Files page: scan ``data-testid="table-row"`` rows; matched by filename
    //   text inside the row spans. Score is keyed by filename via
    //   ``loadScoresByFilename`` (cached in STATE.scoresByFilename).
    // - Module items: similar, filename-based.
    // - Anchor links (Pages, Discussions, embedded etc.): the href carries
    //   ``/files/<id>`` which is the numeric Canvas file_id. Score is keyed
    //   by file_id via ``fetchScoresByFileId``.
    //
    // We fire both lookups in parallel to keep paint snappy. Per-surface
    // results are independent: even if one lookup fails, the others
    // still decorate.
    var rows = findFileRows();
    var moduleItems = findModuleFiles();
    var anchors = findFileLinkAnchors();
    // Accessible-version links (the tool endpoint) inside a published Canvas
    // Page. These are keyed by job id and show the OUTPUT (WCAG) score.
    var accLinks = findAccessibleLinks();

    var byNamePromise = (rows.length || moduleItems.length)
      ? loadScoresByFilename()
      : Promise.resolve({});
    var byIdPromise = anchors.length
      ? fetchScoresByFileId(anchors.map(function (a) { return a.fileId; }))
      : Promise.resolve({});
    var byJobPromise = accLinks.length
      ? fetchScoresByJobId(accLinks.map(function (a) { return a.jobId; }))
      : Promise.resolve({});

    var byName = await byNamePromise;
    var byId = await byIdPromise;
    var byJob = await byJobPromise;

    // Walk up from ``node`` looking for an ancestor that can host an
    // absolute-positioned child. <tr> is unreliable across browsers
    // (most engines treat ``position:relative`` on it as static under the
    // default ``border-collapse:collapse``), so we prefer real cell
    // elements: <td>, <th>, ARIA ``role="cell"`` divs, and Canvas's own
    // ``[data-testid*="cell"]`` divs. Stops at ``stopAt`` (typically the
    // row element) so we never escape upwards into the whole table.
    function findPositioningCell(node, stopAt) {
      var cur = node;
      while (cur && cur !== document.body) {
        if (stopAt && cur === stopAt) break;
        var tag = cur.tagName;
        if (tag === "TD" || tag === "TH") return cur;
        var role = cur.getAttribute && cur.getAttribute("role");
        if (role === "cell" || role === "gridcell") return cur;
        var testid = cur.dataset && cur.dataset.testid;
        if (testid && /cell/i.test(testid)) return cur;
        cur = cur.parentElement;
      }
      return stopAt || null;
    }

    function attachAfter(target, payload, filename, opts) {
      if (!opts && !shouldDecorate(payload)) return;
      var btn = makeDial(payload, filename, opts);
      if (target.parentNode) {
        target.parentNode.insertBefore(btn, target.nextSibling);
      }
    }

    // Small dial-shaped marker for files the watcher hasn't seen yet.
    // Reflow's watcher polls every ~60s; this surfaces that wait so
    // faculty doesn't refresh repeatedly thinking we missed the file.
    // Same actions-cell placement as the real dial for visual
    // consistency.
    function makePendingMarker(filename) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "reflow-pn-dial reflow-pn-pending";
      btn.setAttribute("aria-label",
        "Awaiting accessibility scan for " + filename +
        ". Reflow checks new uploads about once a minute."
      );
      btn.title =
        "Awaiting accessibility scan — Reflow processes new uploads " +
        "automatically (about once a minute). Refresh in a few minutes " +
        "to see results.";
      btn.style.cssText =
        "background:#fff7e6;border:1px solid #cc7a00;color:#6b3a00;" +
        "border-radius:999px;width:52px;height:52px;padding:0;" +
        "display:inline-flex;align-items:center;justify-content:center;" +
        "font-size:18px;cursor:default;";
      btn.innerHTML = '<span aria-hidden="true">⧗</span>';  // hourglass
      // The marker is informational, not interactive — keep clicks
      // from bubbling to the row's row-click handler (which would
      // open the file preview).
      btn.addEventListener("click", function (e) { e.stopPropagation(); e.preventDefault(); });
      return btn;
    }

    function attachPendingMarker(entry) {
      var marker = makePendingMarker(entry.filename);
      var wrap = document.createElement("span");
      wrap.appendChild(marker);
      var actionsCell = entry.row.querySelector('[data-testid="table-cell-actions"]');
      if (actionsCell) {
        wrap.className = "reflow-pn-wrap reflow-pn-actions";
        try {
          if (getComputedStyle(actionsCell).position === "static") {
            actionsCell.style.position = "relative";
          }
          actionsCell.style.overflow = "visible";
        } catch (e) { /* defensive */ }
        actionsCell.appendChild(wrap);
        return;
      }
      var cell = findPositioningCell(entry.filenameSpan || entry.row, entry.row);
      if (cell) {
        wrap.className = "reflow-pn-wrap reflow-pn-cell";
        try {
          if (getComputedStyle(cell).position === "static") {
            cell.style.position = "relative";
          }
        } catch (e) { /* defensive */ }
        cell.appendChild(wrap);
      }
    }

    rows.forEach(function (entry) {
      var payload = byName[entry.filename];
      // No payload AND the user is an instructor: this is a new file
      // the watcher hasn't picked up yet, or one not yet submitted to
      // Reflow. Mark it as "Pending scan" so faculty doesn't think
      // we're ignoring it. Students see nothing in this state — there
      // are no formats for them to act on yet.
      if (!payload && STATE.userRole === "Instructor") {
        entry.row.dataset.reflowDecorated = "1";
        attachPendingMarker(entry);
        return;
      }
      if (!payload) return;
      entry.row.dataset.reflowDecorated = "1";
      if (!shouldDecorate(payload)) return;
      var btn = makeDial(payload, entry.filename);
      var wrap = document.createElement("span");
      wrap.appendChild(btn);
      // Prefer the dedicated actions column so the dial gets its own real
      // estate next to the row's 3-dot menu — keeps it away from the
      // UDOIT "Manage Alternates" dropdown that lives in the name cell.
      // Falls back to the name-cell positioning for surfaces without an
      // actions column (legacy Files page, anchor-only contexts).
      var actionsCell = entry.row.querySelector('[data-testid="table-cell-actions"]');
      if (actionsCell) {
        wrap.className = "reflow-pn-wrap reflow-pn-actions";
        try {
          // Anchor the dial inside the actions cell with absolute positioning
          // so we DON'T disturb the cell's native flex layout for the 3-dot
          // menu — pushing flex into the cell forced the menu off-center on
          // file rows but not folder rows. With absolute positioning the
          // dial floats into the inter-column gutter between Status and
          // Actions and the menu stays right where Canvas put it.
          if (getComputedStyle(actionsCell).position === "static") {
            actionsCell.style.position = "relative";
          }
          actionsCell.style.overflow = "visible";
        } catch (e) { /* defensive */ }
        actionsCell.appendChild(wrap);
        return;
      }
      var cell = findPositioningCell(entry.filenameSpan || entry.row, entry.row);
      if (!cell) return;
      wrap.className = "reflow-pn-wrap reflow-pn-cell";
      try {
        if (getComputedStyle(cell).position === "static") {
          cell.style.position = "relative";
        }
      } catch (e) { /* defensive */ }
      cell.appendChild(wrap);
    });

    moduleItems.forEach(function (entry) {
      var payload = byName[entry.filename];
      if (!payload) return;
      entry.item.dataset.reflowDecorated = "1";
      var anchor = entry.item.querySelector("a") || entry.item.querySelector("span") || entry.item;
      attachAfter(anchor, payload, entry.filename);
    });

    anchors.forEach(function (entry) {
      var payload = byId[entry.fileId];
      if (!shouldDecorate(payload)) return;
      entry.anchor.dataset.reflowDecorated = "1";
      var filename = cleanFilename(entry.anchor.textContent || "Document");
      var cell = findPositioningCell(entry.anchor);
      if (cell) {
        var btn = makeDial(payload, filename);
        var wrap = document.createElement("span");
        wrap.className = "reflow-pn-wrap reflow-pn-cell";
        wrap.appendChild(btn);
        try {
          if (getComputedStyle(cell).position === "static") {
            cell.style.position = "relative";
          }
        } catch (e) { /* defensive */ }
        cell.appendChild(wrap);
        return;
      }
      // Pages / Discussions / arbitrary anchor contexts: no table column to
      // anchor against, fall back to inline-after.
      attachAfter(entry.anchor, payload, filename);
    });

    // Accessible-version links: always decorate (the link IS the accessible
    // output) with the WCAG "after" dial, for faculty and students alike.
    accLinks.forEach(function (entry) {
      var payload = byJob[entry.jobId];
      if (!payload) return;
      entry.anchor.dataset.reflowDecorated = "1";
      var filename = cleanFilename(entry.anchor.textContent || "Document");
      attachAfter(entry.anchor, payload, filename, { accessible: true });
    });
  }

  function scheduleScan() {
    if (scheduleScan._pending) return;
    scheduleScan._pending = true;
    // Coalesce burst mutations into a single scan ~250ms later.
    setTimeout(function () {
      scheduleScan._pending = false;
      decorateAllPdfs().catch(function (e) {
        console.warn("Reflow Panorama scan failed:", e);
      });
    }, 250);
  }

  function init() {
    if (init._started) return;
    init._started = true;
    try { injectStyles(); } catch (e) { console.warn("Reflow Panorama: injectStyles failed", e); }
    try { getEnv(); } catch (e) { console.warn("Reflow Panorama: getEnv failed", e); }
    // Fetch the CSRF token in the background. The first scheduled
    // scan can fire before the token arrives -- that's fine because
    // scans are read-only. State-changing actions check STATE.csrfToken
    // at submit time.
    loadCsrfToken().catch(function (e) {
      console.warn("Reflow Panorama: CSRF fetch failed", e);
    });
    // Check Canvas-OAuth authorization in the background so the modal can
    // surface an "Authorize Reflow" prompt to instructors who haven't yet
    // granted access. Read-only; safe to fire alongside the first scan.
    _installOAuthMessageListener();
    loadOAuthStatus().catch(function (e) {
      console.warn("Reflow Panorama: OAuth status fetch failed", e);
    });
    // Load the AI-API consent state too, so the modal can hide the
    // "Authorization required…" banner for instructors who already accepted
    // (and show "✓ Authorized" in the footer). Without this STATE.consent
    // stayed undefined and the banner showed unconditionally.
    loadConsentStatus().catch(function (e) {
      console.warn("Reflow Panorama: consent status fetch failed", e);
    });
    scheduleScan();
    try {
      var obs = new MutationObserver(scheduleScan);
      obs.observe(document.body, { childList: true, subtree: true });
    } catch (e) {
      console.warn("Reflow Panorama: MutationObserver unavailable", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
