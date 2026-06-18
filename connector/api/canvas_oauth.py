"""HTTP routes for the per-user Canvas OAuth2 flow (Phase 8).

Drives the consent redirect dance:

  * ``GET /canvas/oauth/authorize`` -- requires an active LTI session.
    Builds a CSRF state, stashes (platform, session, return_url) under
    that state, redirects the user's browser to Canvas's
    ``/login/oauth2/auth`` consent page.

  * ``GET /canvas/oauth/callback`` -- Canvas redirects here with
    ``?code=...&state=...`` after consent. We look up the state, swap
    the code for tokens via ``user_oauth.exchange_code``, persist the
    tokens, and redirect the user back to the original return_url.

The actual signing helper (build a JWT bearer assertion against the
platform's token endpoint) lives in ``canvas.oauth`` from the
client_credentials work; we reuse it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jwcrypto import jwk, jwt
from redis.asyncio import Redis

from ..canvas.user_oauth import (
    OAuthState,
    UserOAuthError,
    exchange_code,
    new_oauth_state_token,
    put_user_token,
    put_oauth_state,
    take_oauth_state,
    authorization_url,
)
from ..config import settings
from ..dependencies import get_redis_client
from ..lti.keys import load_private_key
from ..lti.platform import PlatformInstall
from ..lti.platform_store import (
    claim_course_owner_if_unset,
    get_platform,
    get_platform_for_course,
)
from ..lti.session import SESSION_COOKIE, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/canvas/oauth", tags=["canvas-oauth"])


# Scopes the tool requests on the user's behalf. These match the union
# of what the watcher reads and what the bridge writes; user must have
# permission in Canvas for each, otherwise the consent page lists fewer.
_USER_SCOPES = [
    "url:GET|/api/v1/courses/:course_id/files",
    "url:GET|/api/v1/courses/:course_id/folders",
    "url:GET|/api/v1/courses/:course_id/modules",
    "url:GET|/api/v1/courses/:course_id/modules/:module_id/items",
    "url:GET|/api/v1/courses/:course_id/pages",
    "url:GET|/api/v1/courses/:course_id/pages/:url_or_id",
    "url:GET|/api/v1/courses/:course_id/discussion_topics",
    "url:GET|/api/v1/courses/:course_id/discussion_topics/:topic_id/entries",
    "url:GET|/api/v1/courses/:course_id/assignments",
    "url:GET|/api/v1/courses/:course_id/quizzes",
    "url:GET|/api/v1/files/:id",
    "url:GET|/api/v1/folders/:id/files",
    "url:POST|/api/v1/courses/:course_id/pages",
    "url:PUT|/api/v1/courses/:course_id/pages/:url_or_id",
    "url:POST|/api/v1/conversations",
    # Upload converted figures into a course folder so generated pages embed
    # Canvas-hosted images (self-contained; no figure proxy).
    "url:POST|/api/v1/courses/:course_id/files",
]


def _build_redirect_uri(request: Request) -> str:
    """Compute the absolute callback URL Canvas should redirect to.

    Behind ngrok/Cloudflare the auto-derived URL is wrong (scheme=http
    when actually https). Honor ``LTI_PUBLIC_URL`` when set, same logic
    as the LTI launch handler.
    """
    public_url = (getattr(settings, "lti_public_url", None) or "").rstrip("/")
    if public_url:
        return f"{public_url}/canvas/oauth/callback"
    return str(request.url_for("canvas_oauth_callback"))


def _client_assertion_for_token_endpoint(platform: PlatformInstall) -> str:
    """Build a JWT bearer client assertion for the code/refresh exchanges.

    Canvas accepts the same JWT bearer client-authentication scheme for
    the authorization_code flow as it does for client_credentials. We
    reuse the existing private key + JWKS infrastructure.
    """
    private_key = load_private_key()
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key = jwk.JWK.from_pem(pem)
    kid = key.thumbprint()

    import secrets
    now = int(time.time())
    # Same canonical-SSO aud rule as oauth.py service-token client.
    audience = (platform.issuer or "").rstrip("/") + "/login/oauth2/token"
    claims: dict[str, Any] = {
        "iss": platform.client_id,
        "sub": platform.client_id,
        "aud": audience,
        "iat": now,
        "exp": now + 300,
        "jti": secrets.token_urlsafe(32),
    }
    token = jwt.JWT(
        header={"alg": "RS256", "typ": "JWT", "kid": kid},
        claims=claims,
    )
    token.make_signed_token(key)
    return token.serialize()


async def _platform_for_session(redis: Redis, session) -> PlatformInstall | None:
    """Look up which Canvas platform this LTI session belongs to.

    The session payload doesn't store platform_id directly; we derive it
    from the course-to-platform map populated by the launch handler.
    """
    if not session.course_id:
        return None
    platform_id = await get_platform_for_course(redis, session.course_id)
    if not platform_id:
        return None
    return await get_platform(redis, platform_id)


@router.get("/authorize")
async def authorize(
    request: Request,
    return_url: str = Query("/", description="Where to redirect after consent"),
    popup: int = Query(0, description="1 when opened in a popup window"),
    redis: Redis = Depends(get_redis_client),
    reflow_lti_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> RedirectResponse:
    """Kick off the OAuth2 dance. Requires an active LTI session cookie."""

    if not reflow_lti_session:
        raise HTTPException(status_code=401, detail="No LTI session")
    session = await get_session(redis, reflow_lti_session)
    if session is None:
        raise HTTPException(status_code=401, detail="LTI session expired")

    platform = await _platform_for_session(redis, session)
    if platform is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot resolve Canvas platform for this session. "
                   "Re-launch via the LTI link.",
        )

    is_popup = bool(popup)
    state = new_oauth_state_token()
    await put_oauth_state(
        redis,
        state,
        OAuthState(
            platform_id=platform.platform_id,
            session_id=reflow_lti_session,
            return_url=return_url,
            popup=is_popup,
        ),
    )

    redirect_uri = _build_redirect_uri(request)
    url = authorization_url(
        platform,
        redirect_uri=redirect_uri,
        state=state,
        scopes=_USER_SCOPES,
    )
    logger.info(
        "OAuth authorize: user=%s course=%s platform=%s popup=%s",
        session.user_id, session.course_id, platform.platform_id, is_popup,
    )

    # Popup mode: the window opened by the overlay is already a top-level
    # browser window (not inside Canvas's LTI iframe), so there's nothing to
    # break out of — redirect straight to Canvas's consent page. This avoids
    # the intermediate "Redirecting…" flash and keeps the user in a small
    # popup that closes itself after the callback.
    if is_popup:
        return RedirectResponse(url=url, status_code=302)

    # Canvas's /login/oauth2/auth consent page sets X-Frame-Options=DENY
    # for security reasons. Our LTI launch lives inside Canvas's tool
    # iframe, so a plain 302 redirect dead-ends in the iframe with a
    # browser-blocked load. Break out to the top-level window via JS so
    # the consent page can render in the user's main browser context.
    # After consent + callback, we send the user back to the original
    # LTI launch URL inside Canvas; Canvas re-launches Reflow, our
    # handler sees the stored user_token, and skips OAuth.
    import html as _h
    safe_url = _h.escape(url, quote=True)
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Authorizing Reflow...</title>
<style>body{{font:14px system-ui,sans-serif;padding:2rem;color:#1d1d1d;}}
a{{color:#0a5fb5;}}</style></head><body>
<p>Redirecting to Canvas for one-time authorization...</p>
<p>If you are not redirected automatically,
<a href="{safe_url}" target="_top" rel="noopener">click here to continue</a>.</p>
<script>
  // Break out of Canvas's LTI iframe. Without this, Canvas's consent
  // page would refuse to render due to X-Frame-Options.
  try {{
    if (window.top && window.top !== window.self) {{
      window.top.location.href = {repr(url)};
    }} else {{
      window.location.href = {repr(url)};
    }}
  }} catch (e) {{
    // Cross-origin parent. Fall back to a noopener top-level nav from
    // a click; show the link prominently.
    window.location.href = {repr(url)};
  }}
</script>
</body></html>"""
    return HTMLResponse(content=body, status_code=200)


@router.get("/callback", name="canvas_oauth_callback")
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    redis: Redis = Depends(get_redis_client),
) -> RedirectResponse:
    """Canvas redirects here after the user approves consent."""

    if error:
        logger.warning(
            "OAuth callback received error: %s (%s)", error, error_description,
        )
        # Friendly bounce-back to a static page would be nicer; for the
        # MVP we surface the raw error so faculty can show IT what went
        # wrong.
        raise HTTPException(
            status_code=400,
            detail=f"Canvas OAuth declined: {error} ({error_description})",
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    stashed = await take_oauth_state(redis, state)
    if stashed is None:
        raise HTTPException(status_code=400, detail="Unknown or expired state")

    platform = await get_platform(redis, stashed.platform_id)
    if platform is None:
        raise HTTPException(status_code=400, detail="Unknown platform in state")

    session = await get_session(redis, stashed.session_id)
    if session is None:
        # User let too much time pass between launch and consent; the
        # launch session cookie has aged out. Send them back through.
        raise HTTPException(
            status_code=401,
            detail="LTI session expired during OAuth handshake; please re-launch.",
        )

    redirect_uri = _build_redirect_uri(request)
    # Pick client-authentication scheme based on which dev key the
    # operator configured:
    #   * Dual-key model (CANVAS_OAUTH_CLIENT_SECRET set in env) -- the
    #     OAuth-side dev key is a non-LTI "API Key" that Canvas registers
    #     WITHOUT a JWK URL. It only accepts the shared-secret POST scheme.
    #     Sending a JWT bearer assertion here yields invalid_client because
    #     Canvas has no JWK on file to verify it against.
    #   * Single-key (legacy / LTI-key for both launch and OAuth) -- use
    #     the JWT bearer assertion path, signed with our LTI private key,
    #     which Canvas verifies against the dev key's public_jwk_url.
    oauth_secret = getattr(settings, "canvas_oauth_client_secret", None)
    secret_val = ""
    if oauth_secret is not None:
        secret_val = (
            oauth_secret.get_secret_value()
            if hasattr(oauth_secret, "get_secret_value")
            else str(oauth_secret)
        )
    try:
        if secret_val:
            token = await exchange_code(
                platform,
                code=code,
                redirect_uri=redirect_uri,
                client_secret=secret_val,
            )
        else:
            assertion = _client_assertion_for_token_endpoint(platform)
            token = await exchange_code(
                platform,
                code=code,
                redirect_uri=redirect_uri,
                client_assertion=assertion,
            )
    except UserOAuthError as exc:
        logger.exception("Canvas code exchange failed for user=%s", session.user_id)
        raise HTTPException(
            status_code=502,
            detail=f"Canvas token exchange failed: {exc}",
        ) from exc

    # Bind the canvas user identity from the session if Canvas didn't echo
    # it on the response (which happens when ``scope`` is empty).
    if not token.canvas_user_id:
        token.canvas_user_id = session.user_id

    await put_user_token(
        redis, platform.platform_id, session.user_id, token,
    )
    logger.info(
        "OAuth callback: stored user_token for platform=%s user=%s scopes=%d",
        platform.platform_id, session.user_id, len(_USER_SCOPES),
    )

    # Claim course ownership if this user is an Instructor and no
    # owner is currently assigned. The watcher uses this owner to
    # authorize background scans against the course. First-write-wins
    # via Redis SET NX so two faculty completing consent near-simultaneously
    # don't race over each other.
    is_instructor = any(
        "Instructor" in r or "Teacher" in r or "TeachingAssistant" in r
        for r in (session.roles or [])
    )
    if is_instructor and session.course_id:
        claimed = await claim_course_owner_if_unset(
            redis, course_id=session.course_id, user_id=session.user_id,
        )
        if claimed:
            logger.info(
                "Course %s owner claimed by user=%s; watcher will use their token",
                session.course_id, session.user_id,
            )

    # Popup mode: the overlay opened this flow in a small window. Don't
    # navigate it anywhere — tell the opener (the Canvas page running the
    # overlay) that authorization succeeded, then close ourselves. The
    # opener re-checks status and refreshes its badges. ``postMessage``
    # targets ``*`` because the opener is on the Canvas origin, which differs
    # from this tool's origin; the overlay validates the message ``type``.
    if stashed.popup:
        body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Reflow authorized</title>
<style>body{font:14px system-ui,sans-serif;padding:2rem;color:#1d1d1d;text-align:center;}</style>
</head><body>
<p>✓ Reflow is authorized. You can close this window.</p>
<script>
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage({ type: "reflow-oauth", ok: true }, "*");
    }
  } catch (e) {}
  setTimeout(function () { try { window.close(); } catch (e) {} }, 300);
</script>
</body></html>"""
        return HTMLResponse(content=body, status_code=200)

    return RedirectResponse(url=stashed.return_url or "/", status_code=302)
