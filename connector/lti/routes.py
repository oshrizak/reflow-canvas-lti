"""LTI 1.3 HTTP endpoints.

Three stops on the launch dance plus a public config:

  * ``/lti/login``  - Canvas redirects the browser here first (OIDC login).
  * ``/lti/launch`` - Canvas POSTs the signed id_token; we validate and
                       redirect into the review UI.
  * ``/lti/jwks``   - Public key set Canvas uses to verify *our* outgoing
                       service assertions (LTI Advantage, future use).
  * ``/lti/config.json`` - Tool config Canvas admin can paste when
                            registering the Developer Key.

The actual review screens live under ``/canvas/review/*`` (see
``connector.api.canvas_review``); this module only handles the LTI handshake.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from redis.asyncio import Redis

from ..config import settings
from ..dependencies import get_redis_client
from .config import get_lti_settings
from .jwt_validation import LtiValidationError, validate_launch_jwt
from .keys import jwks_document
from .platform import build_install_from_launch
from .platform_store import mark_course_seen, put_platform
from .session import (
    SESSION_COOKIE,
    SessionPayload,
    StatePayload,
    new_session_id,
    new_state_token,
    put_session,
    put_state,
    take_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lti", tags=["lti"])


def _require_enabled() -> None:
    if not get_lti_settings().enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="LTI integration disabled",
        )


@router.post("/login")
@router.get("/login")
async def login(
    request: Request,
    redis: Redis = Depends(get_redis_client),
) -> RedirectResponse:
    """OIDC login initiation.

    Canvas calls this either as GET (with query params) or POST (form
    body) depending on the placement. Both shapes carry the same fields.
    """

    _require_enabled()
    params: dict[str, str] = {}
    if request.method == "POST":
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
    else:
        params = dict(request.query_params)

    iss = params.get("iss", "")
    login_hint = params.get("login_hint", "")
    lti_message_hint = params.get("lti_message_hint")
    target_link_uri = params.get("target_link_uri", "")
    client_id = params.get("client_id", "")

    cfg = get_lti_settings()
    if iss != cfg.issuer:
        logger.warning(
            "LTI login issuer mismatch: got=%r expected=%r (client_id=%r)",
            iss, cfg.issuer, client_id,
        )
        raise HTTPException(status_code=400, detail="Unknown issuer")
    if client_id and client_id != cfg.client_id:
        raise HTTPException(status_code=400, detail="Unexpected client_id")

    state = new_state_token()
    nonce = new_state_token()
    payload = StatePayload(
        nonce=nonce,
        login_hint=login_hint,
        lti_message_hint=lti_message_hint,
        target_link_uri=target_link_uri,
        issuer=iss,
        client_id=cfg.client_id,
    )
    await put_state(redis, payload)

    # Build the redirect_uri from LTI_PUBLIC_URL when available. The dev
    # uvicorn doesn't pass --proxy-headers, so request.url_for() builds an
    # http:// URL behind ngrok/Cloudflare and Canvas rejects the mismatch.
    public_url = (getattr(settings, "lti_public_url", None) or "").rstrip("/")
    if public_url:
        redirect_uri = f"{public_url}/lti/launch"
    else:
        redirect_uri = str(request.url_for("lti_launch"))

    query = urlencode(
        {
            "scope": "openid",
            "response_type": "id_token",
            "response_mode": "form_post",
            "prompt": "none",
            "client_id": cfg.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "login_hint": login_hint,
            "lti_message_hint": lti_message_hint or "",
        }
    )
    return RedirectResponse(url=f"{cfg.auth_login_url}?{query}", status_code=302)


@router.post("/launch", name="lti_launch")
async def launch(
    request: Request,
    id_token: str = Form(...),
    state: str = Form(...),
    redis: Redis = Depends(get_redis_client),
) -> RedirectResponse:
    """Receive the signed launch token from Canvas, validate, hand off."""

    _require_enabled()
    if not state:
        raise HTTPException(status_code=400, detail="Missing state")

    try:
        claims = await validate_launch_jwt(id_token)
    except LtiValidationError as exc:
        logger.warning("LTI launch validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid LTI launch") from exc

    nonce = claims.raw.get("nonce", "")
    stored = await take_state(redis, nonce) if nonce else None
    if stored is None:
        raise HTTPException(status_code=400, detail="Unknown or expired state")

    # Multi-tenant Phase 1: persist a PlatformInstall for this Canvas
    # instance so Phase 2's service-token client has somewhere to read
    # endpoint + identity info from. The label comes from the launch's
    # tool_platform claim when present; otherwise we'll just see the
    # bare canvas_domain in ops dashboards.
    install = None  # may stay None if the upsert fails; treated as no-platform below
    try:
        tool_platform = claims.raw.get(
            "https://purl.imsglobal.org/spec/lti/claim/tool_platform", {}
        )
        label_parts: list[str] = []
        for key in ("name", "product_family_code"):
            v = tool_platform.get(key) if isinstance(tool_platform, dict) else None
            if v:
                label_parts.append(str(v))
        label = " - ".join(label_parts) if label_parts else None

        # Canvas Cloud sends the institution's API hostname via the
        # canvas_api_domain custom substitution. When present, we override
        # the SSO-derived canvas_api_base so user-OAuth and data API calls
        # land on the right host without any manual config.
        custom = claims.raw.get(
            "https://purl.imsglobal.org/spec/lti/claim/custom", {}
        ) or {}
        api_domain_override = (
            custom.get("canvas_api_domain") if isinstance(custom, dict) else None
        )

        install = build_install_from_launch(
            issuer=claims.issuer,
            client_id=claims.audience,
            deployment_id=claims.deployment_id,
            label=label,
        )
        # Single-tenant override always wins. Canvas Cloud's prod issuer
        # (`canvas.instructure.com`) ALSO fronts the discontinued
        # Free-for-Teacher tenant, so auto-derivation from issuer ends up
        # pointing OAuth at FFT. If Canvas does send the `canvas_api_domain`
        # custom claim, it may carry the issuer-host (canvas.instructure.com)
        # rather than the institutional host (csueb.instructure.com). When
        # CANVAS_API_URL is configured in .env, prefer it as the
        # authoritative institutional host so user-OAuth and REST calls
        # land on the tenant the operator actually deployed for.
        api_url_env = (getattr(settings, "canvas_api_url", "") or "").strip()
        parsed_env_host = ""
        if api_url_env:
            parsed_env = urlparse(api_url_env)
            parsed_env_host = (parsed_env.netloc or "").strip()
        if parsed_env_host and (
            parsed_env_host.endswith(".instructure.com")
            or parsed_env_host.endswith(".canvaslms.com")
        ):
            install.canvas_api_base = f"https://{parsed_env_host}/api/v1"
            install.canvas_domain = parsed_env_host
        elif api_domain_override:
            host = str(api_domain_override).strip()
            # Defensive: only accept Canvas-shaped hostnames so a hostile
            # platform can't redirect our API calls to an attacker.
            if host.endswith(".instructure.com") or host.endswith(".canvaslms.com"):
                install.canvas_api_base = f"https://{host}/api/v1"
                install.canvas_domain = host
        await put_platform(redis, install)
        # Tie this course to its platform so the multi-tenant watcher
        # (Phase 5) knows which credentials to use when scanning it.
        if claims.course_id:
            await mark_course_seen(redis, install.platform_id, claims.course_id)
    except Exception:  # pragma: no cover - never break launches over a registry write
        logger.exception(
            "PlatformInstall upsert failed for issuer=%r deployment_id=%r; "
            "launch continues without registry record",
            claims.issuer, claims.deployment_id,
        )

    session_id = new_session_id()
    await put_session(
        redis,
        session_id,
        SessionPayload(
            user_id=claims.user_id,
            user_name=claims.user_name,
            user_email=claims.user_email,
            course_id=claims.course_id,
            roles=claims.roles,
        ),
    )

    # Default landing page: the review queue for this course. Honour the
    # target_link_uri if Canvas told us where to go - UNLESS it points at
    # our own /lti/launch endpoint (a self-referential loop that happens
    # when the LTI tool config sets target_link_uri to the launch URL).
    # In that case fall back to the per-course review page.
    raw_target = (claims.target_link_uri or "").strip()
    if raw_target and raw_target.rstrip("/").endswith("/lti/launch"):
        raw_target = ""
    next_url = raw_target or f"/canvas/review?course_id={claims.course_id}"

    # Faculty consent / disclaimer gate.
    from ..canvas.state import get_consent, needs_consent

    consent = await get_consent(redis, claims.user_id)
    if needs_consent(consent):
        from urllib.parse import quote
        next_url = f"/canvas/consent?next={quote(next_url, safe='')}"

    # Phase 8: Canvas OAuth2 user-token gate. Canvas Cloud's general
    # REST API requires per-user OAuth-issued tokens (not LTI Advantage
    # service tokens), so the first time a faculty member launches the
    # tool we send them through Canvas's consent screen before letting
    # them reach the review UI. Subsequent launches see a stored token
    # and skip the redirect. Tokens auto-refresh on use.
    existing_user_token = None
    if install is not None:
        try:
            from ..canvas.user_oauth import get_user_token

            existing_user_token = await get_user_token(
                redis, install.platform_id, claims.user_id,
            )
        except Exception:
            existing_user_token = None
    if install is not None and existing_user_token is None:
        from urllib.parse import quote
        # After OAuth consent, the user will be at top-level (we broke
        # out of Canvas's iframe to do consent). Bouncing back to a
        # Reflow path like /canvas/review would render outside Canvas's
        # UI shell. Send them to the Canvas course URL instead so
        # Canvas re-renders with its nav, and they click the Reflow
        # tool a second time -- now with a stored user_token so the
        # OAuth gate is skipped and the launch goes straight to the
        # review UI in the iframe.
        canvas_return = (
            f"https://{install.canvas_domain}/courses/{claims.course_id}"
            if install.canvas_domain and claims.course_id
            else next_url
        )
        next_url = (
            f"/canvas/oauth/authorize?return_url={quote(canvas_return, safe='')}"
        )

    resp = RedirectResponse(url=next_url, status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=8 * 3600,
        httponly=True,
        secure=True,
        samesite="none",
    )
    return resp


@router.get("/jwks")
async def jwks() -> JSONResponse:
    """Public JWKS document Canvas fetches to verify our assertions."""

    _require_enabled()
    return JSONResponse(jwks_document())


@router.get("/config.json")
async def tool_config(request: Request) -> JSONResponse:
    """JSON config an admin pastes when creating a Developer Key.

    Canvas's "Paste JSON" flow expects this exact shape. See
    https://canvas.instructure.com/doc/api/file.lti_dev_key_config.html.
    """

    _require_enabled()
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "title": "Equalify Reflow - Accessible Documents",
            "description": (
                "Automatically converts PDFs uploaded to Canvas into "
                "accessible, reflowable Canvas Pages."
            ),
            "oidc_initiation_url": f"{base}/lti/login",
            "target_link_uri": f"{base}/canvas/review",
            # Canvas's LTI key generator pre-populates Redirect URIs from
            # ``target_link_uri`` only. The OIDC handshake redirects to
            # ``/lti/launch`` though, so without this explicit list Canvas
            # rejects the post-login redirect with "Invalid redirect_uri".
            "redirect_uris": [
                f"{base}/lti/launch",
                f"{base}/canvas/review",
            ],
            "scopes": [
                # ONLY IMS-standard LTI Advantage scopes belong on the LTI
                # Developer Key. Canvas Cloud rejects ``url:GET|/api/v1/...``
                # / ``url:POST|/api/v1/...`` shapes in this config — those
                # are Canvas data-API scopes and they live on the separate
                # *API* Developer Key (which faculty consent to via the
                # per-instructor OAuth2 flow at /canvas/oauth/authorize).
                #
                # Order is alphabetical for stability.
                "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly",
                "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly",
            ],
            "extensions": [
                {
                    "domain": request.url.hostname or "",
                    "tool_id": "equalify-reflow",
                    "platform": "canvas.instructure.com",
                    "settings": {
                        "text": "Accessible Documents",
                        "placements": [
                            {
                                "placement": "course_navigation",
                                "message_type": "LtiResourceLinkRequest",
                                "text": "Accessible Documents",
                                "target_link_uri": f"{base}/canvas/review",
                                "default": "enabled",
                                "visibility": "members",
                            }
                        ],
                    },
                }
            ],
            "public_jwk_url": f"{base}/lti/jwks",
            "custom_fields": {
                "course_id": "$Canvas.course.id",
                "user_id": "$Canvas.user.id",
                # Institutional host (e.g. csueb.test.instructure.com). Canvas
                # Cloud's LTI issuer is the shared SSO host, but the actual
                # API host is the per-institution subdomain. We capture the
                # subdomain via this substitution so platform records get the
                # right URL without manual ops scripts per school.
                "canvas_api_domain": "$Canvas.api.domain",
            },
        }
    )


@router.get("/healthz")
async def healthz() -> HTMLResponse:
    """Lightweight check Canvas admins can hit to confirm the tool is up."""

    cfg = get_lti_settings()
    body = "<h1>LTI tool</h1><p>enabled: {}</p>".format("yes" if cfg.enabled else "no")
    return HTMLResponse(body)


@router.get("/panorama.js")
async def panorama_js() -> Response:
    """Serve the Theme-Editor JS bundle that Canvas injects on every page."""
    from pathlib import Path
    bundle_path = Path(__file__).resolve().parent.parent / "web" / "canvas_review" / "panorama.js"
    if not bundle_path.exists():
        return Response(status_code=404, content="// panorama.js bundle not found")
    return Response(
        content=bundle_path.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
