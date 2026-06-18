"""Faculty consent / disclaimer flow.

On first launch (or after a disclaimer-version bump) the LTI launch handler
redirects faculty here. They acknowledge the data-handling disclaimer; we
record the acknowledgment + an append-only audit entry in Redis; subsequent
launches skip the prompt until the version changes again.

Why a consent flow:
  - Explicit user consent is a recognized mitigation under FERPA, NIST AI RMF
    (Govern function), and most university AI / data-governance policies.
  - It shortens InfoSec review because the data-handling story changes from
    "silent automatic processing" to "documented user-authorized processing."
  - Audit log gives the ISO a clean answer to "who agreed to what, when, and
    from where."
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from redis.asyncio import Redis

from ..canvas.state import (
    CURRENT_CONSENT_VERSION,
    ConsentRecord,
    get_consent,
    needs_consent,
    put_consent,
    revoke_consent,
)
from ..dependencies import get_redis_client
from ..lti.routes import SESSION_COOKIE
from ..lti.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/canvas/consent", tags=["canvas-consent"])


# ---------------------------------------------------------------------------
# Helper: resolve current LTI session → user_id, or 401
# ---------------------------------------------------------------------------
async def _current_session(
    redis: Redis,
    session_id: str | None,
) -> Any:
    if not session_id:
        raise HTTPException(status_code=401, detail="No LTI session")
    sess = await get_session(redis, session_id)
    if sess is None:
        raise HTTPException(status_code=401, detail="Expired LTI session")
    return sess


# ---------------------------------------------------------------------------
# GET /canvas/consent  →  HTML page (the disclaimer)
# ---------------------------------------------------------------------------
# Security headers applied to every HTML response from this module.
# - ``Content-Security-Policy`` locks the page down to its own origin with
#   inline styles (the consent template ships its own CSS in a <style>
#   block; no external CSS/JS is loaded). ``frame-ancestors`` lets Canvas
#   embed this page inside its LTI iframe but blocks every other origin.
# - ``X-Content-Type-Options`` / ``X-Frame-Options`` / ``Referrer-Policy``
#   are belt-and-suspenders for older user agents that ignore CSP.
# - ``Strict-Transport-Security`` is set only in production deployments
#   (operators control this via a reverse proxy; we don't enforce here so
#   localhost dev keeps working).
CONSENT_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors https://*.instructure.com https://*.csueb.edu "
        "https://*.csueastbay.edu https://*.ngrok-free.dev https://*.ngrok.io; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "ALLOW-FROM https://canvas.instructure.com",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@router.get("", response_class=HTMLResponse)
async def consent_page(
    request: Request,
    next: str = "/canvas/review",
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> HTMLResponse:
    """Render the consent / authorization page for the current LTI session."""
    sess = await _current_session(redis, session_id)
    existing = await get_consent(redis, sess.user_id)
    already_consented = existing is not None and not needs_consent(existing)
    return HTMLResponse(
        _render_page(
            user_name=sess.user_name or "",
            next_url=next,
            already_consented=already_consented,
            current_version=CURRENT_CONSENT_VERSION,
            previous_version=existing.version if existing else None,
        ),
        headers=CONSENT_SECURITY_HEADERS,
    )


# ---------------------------------------------------------------------------
# POST /canvas/consent  →  record consent, redirect to next_url
# ---------------------------------------------------------------------------
@router.post("")
async def consent_submit(
    request: Request,
    agree_processing: str = Form(default=""),
    agree_pii: str = Form(default=""),
    agree_responsibility: str = Form(default=""),
    next_url: str = Form(default="/canvas/review"),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> RedirectResponse:
    """Record the faculty acknowledgment and redirect back into the app."""
    sess = await _current_session(redis, session_id)

    # All three checkboxes must be checked. If any is missing, re-render the
    # page with an error rather than recording a partial consent.
    if not (agree_processing and agree_pii and agree_responsibility):
        raise HTTPException(
            status_code=400,
            detail="You must agree to all three clauses to use Reflow.",
        )

    record = ConsentRecord(
        user_id=sess.user_id,
        user_name=sess.user_name,
        user_email=sess.user_email,
        course_id=sess.course_id,
        version=CURRENT_CONSENT_VERSION,
        agreed_at=time.time(),
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    await put_consent(redis, record)
    logger.info(
        "Consent recorded: user=%s version=%s course=%s",
        sess.user_id,
        CURRENT_CONSENT_VERSION,
        sess.course_id,
    )
    # 303 forces a GET on the next URL (we just POSTed)
    return RedirectResponse(url=next_url, status_code=303)


# ---------------------------------------------------------------------------
# GET /canvas/consent/status  →  JSON probe used by panorama.js
# ---------------------------------------------------------------------------
@router.get("/status")
async def consent_status(
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Return { agreed: bool, version, agreed_at } for the current user.

    Used by the front-end overlay to decide whether to show the small footer
    reminder vs the full consent modal (or to skip the dial entirely until
    the user consents).
    """
    if not session_id:
        # Public probe — return 'unknown' rather than 401 so the front-end
        # can degrade gracefully when the overlay loads outside an LTI launch.
        return JSONResponse({"agreed": False, "reason": "no_session"})
    sess = await get_session(redis, session_id)
    if sess is None:
        return JSONResponse({"agreed": False, "reason": "expired_session"})
    record = await get_consent(redis, sess.user_id)
    if record is None:
        return JSONResponse({
            "agreed": False,
            "reason": "never_consented",
            "current_version": CURRENT_CONSENT_VERSION,
        })
    if needs_consent(record):
        return JSONResponse({
            "agreed": False,
            "reason": "version_changed",
            "previous_version": record.version,
            "current_version": CURRENT_CONSENT_VERSION,
        })
    return JSONResponse({
        "agreed": True,
        "version": record.version,
        "agreed_at": record.agreed_at,
        "current_version": CURRENT_CONSENT_VERSION,
    })


# ---------------------------------------------------------------------------
# POST /canvas/consent/revoke  →  admin operation
# ---------------------------------------------------------------------------
@router.post("/revoke")
async def consent_revoke(
    user_id: str = Form(...),
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    redis: Redis = Depends(get_redis_client),
) -> JSONResponse:
    """Revoke a user's consent. Caller must be an admin in the LTI session.

    The audit log keeps the original consent + a revoke marker so the full
    history is preserved.
    """
    sess = await _current_session(redis, session_id)
    is_admin = any(
        r.endswith("Administrator") or r.endswith("Instructor") or "Admin" in r
        for r in (sess.roles or [])
    )
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    await revoke_consent(redis, user_id)
    logger.info("Consent revoked by %s for user %s", sess.user_id, user_id)
    return JSONResponse({"revoked": user_id})


# ---------------------------------------------------------------------------
# Page renderer — accessible, branded, no external CSS/JS deps
# ---------------------------------------------------------------------------
def _render_page(
    *,
    user_name: str,
    next_url: str,
    already_consented: bool,
    current_version: str,
    previous_version: str | None,
) -> str:
    safe_user = (user_name or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_next = (next_url or "/canvas/review").replace('"', "&quot;")
    banner = ""
    if already_consented:
        banner = (
            '<div role="status" class="banner ok">You have already '
            "acknowledged the current disclaimer. You can proceed.</div>"
        )
    elif previous_version is not None:
        banner = (
            f'<div role="status" class="banner warn">The disclaimer was '
            f"updated (v{previous_version} → v{current_version}). Please "
            "re-acknowledge before continuing.</div>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Reflow — Authorization &amp; Disclaimer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      --bg: #f7f9fc;
      --card: #ffffff;
      --ink: #1a2233;
      --muted: #5d6b85;
      --accent: #1f4e79;
      --accent-hover: #163960;
      --warn-bg: #fff4e5;
      --warn-border: #f0b878;
      --ok-bg: #e8f1e4;
      --ok-border: #7cb56e;
      --border: #d8dee9;
      --error: #b3261e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 0;
      background: var(--bg);
      font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      color: var(--ink);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    main {{
      max-width: 760px;
      width: 100%;
      margin: 24px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.06);
      padding: 32px 36px;
    }}
    h1 {{
      font-size: 24px;
      margin: 0 0 8px;
      color: var(--accent);
    }}
    .lede {{ color: var(--muted); margin: 0 0 20px; }}
    .banner {{
      padding: 12px 14px;
      border-radius: 6px;
      margin-bottom: 18px;
      font-size: 14px;
    }}
    .banner.ok {{ background: var(--ok-bg); border: 1px solid var(--ok-border); }}
    .banner.warn {{ background: var(--warn-bg); border: 1px solid var(--warn-border); }}
    h2 {{ font-size: 16px; margin: 22px 0 8px; }}
    ul {{ margin: 0 0 16px 22px; padding: 0; }}
    li {{ margin-bottom: 6px; }}
    .checks {{
      margin: 24px 0 16px;
      padding: 18px 20px;
      background: #f4f7fb;
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    .checks label {{
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 8px 0;
      cursor: pointer;
    }}
    .checks input[type=checkbox] {{
      margin-top: 4px;
      width: 18px;
      height: 18px;
      flex: 0 0 18px;
      cursor: pointer;
    }}
    .checks input[type=checkbox]:focus-visible {{
      outline: 3px solid var(--accent);
      outline-offset: 2px;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      align-items: center;
      margin-top: 8px;
    }}
    button.primary {{
      background: var(--accent);
      color: #fff;
      border: none;
      padding: 10px 22px;
      border-radius: 6px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }}
    button.primary:hover {{ background: var(--accent-hover); }}
    button.primary:disabled {{
      background: #b8c2d2;
      cursor: not-allowed;
    }}
    a.cancel {{
      color: var(--muted);
      text-decoration: none;
      font-size: 14px;
    }}
    a.cancel:hover {{ text-decoration: underline; }}
    .meta {{
      margin-top: 28px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 13px;
    }}
    code {{
      background: #ecf0f6;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 13px;
    }}
    #error {{ color: var(--error); font-size: 14px; min-height: 1em; }}
  </style>
</head>
<body>
  <main role="main">
    <h1>Authorization &amp; Disclaimer</h1>
    <p class="lede">Welcome{(' ' + safe_user) if safe_user else ''}. Before using
       Reflow, please review and acknowledge the items below.</p>
    {banner}

    <h2>What Reflow does</h2>
    <ul>
      <li>Reflow processes course materials <strong>you upload to Canvas</strong>
          to generate accessible versions (HTML, ePub, audio, OCR'd PDF,
          Braille, plain text, translations) for students.</li>
      <li>Documents are scanned for personally identifiable information
          (names, SSNs, addresses, phone numbers) and PII is
          <strong>redacted</strong> before any content is sent to the Claude API.</li>
      <li>Content is processed by Anthropic's Claude API to produce the
          accessible output. Anthropic <strong>does not use this content for
          model training</strong>.</li>
      <li>An audit log records when you use the tool and what files were
          processed. Faculty edits to accessible HTML are versioned.</li>
    </ul>

    <h2>What you are agreeing to</h2>
    <form id="consent-form" method="POST" action="/canvas/consent">
      <input type="hidden" name="next_url" value="{safe_next}">
      <div class="checks" role="group" aria-labelledby="checks-label">
        <span id="checks-label" class="sr-only">Acknowledgment checkboxes</span>
        <label>
          <input type="checkbox" name="agree_processing" value="yes" required>
          <span>I authorize Reflow to process documents I upload to Canvas
                through the Claude API for the purpose of generating accessible
                formats.</span>
        </label>
        <label>
          <input type="checkbox" name="agree_pii" value="yes" required>
          <span>I understand that PII detection runs locally before content
                leaves CSUEB systems, but I will avoid uploading documents
                whose primary purpose is to convey sensitive personal data
                (graded papers with feedback, rosters, etc.).</span>
        </label>
        <label>
          <input type="checkbox" name="agree_responsibility" value="yes" required>
          <span>I remain responsible for the content of materials I upload.
                Reflow is an accessibility-conversion assistant, not a content
                review service, and I will review the generated outputs before
                publishing them to students.</span>
        </label>
      </div>
      <p id="error" aria-live="polite"></p>
      <div class="actions">
        <button type="submit" class="primary" id="agree-btn" disabled>
          I Agree &amp; Continue
        </button>
        <a class="cancel" href="about:blank" onclick="window.history.back();return false;">
          Cancel
        </a>
      </div>
    </form>

    <div class="meta">
      Disclaimer version <code>v{current_version}</code>. You may revoke this
      authorization by contacting your campus LMS administrator. A complete
      record of when you acknowledged this notice (and from what IP address)
      is kept for compliance review.
    </div>
  </main>
  <script>
    (function() {{
      var form = document.getElementById("consent-form");
      var btn = document.getElementById("agree-btn");
      var boxes = form.querySelectorAll('input[type=checkbox]');
      function refresh() {{
        var all = true;
        boxes.forEach(function(b) {{ if (!b.checked) all = false; }});
        btn.disabled = !all;
      }}
      boxes.forEach(function(b) {{ b.addEventListener("change", refresh); }});
      refresh();
    }})();
  </script>
</body>
</html>"""
