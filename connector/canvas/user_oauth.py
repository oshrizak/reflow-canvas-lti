"""Per-user Canvas OAuth2 access tokens (Phase 8).

Canvas Cloud's LTI Advantage client-credentials tokens are not honored by
the standard ``/api/v1/...`` REST API -- they only work for LTI Advantage
services (NRPS, AGS, Deep Linking). For general Canvas API access (file
listings, page creation, conversations) the only Canvas-supported path
is **per-user OAuth2**: each faculty member authorizes the tool once,
and the tool stores a (short-lived access_token + long-lived
refresh_token) pair to make API calls on that user's behalf.

This module handles the OAuth2 dance and the resulting token storage.
The actual HTTP routes that drive the redirects live in
``connector.api.canvas_oauth``; this module is the pure logic layer.

Flow:

  1. ``authorization_url(platform, redirect_uri, state, scopes)`` builds
     the Canvas auth-page URL the user is redirected to.
  2. After consent Canvas redirects to ``redirect_uri?code=...&state=...``.
  3. ``exchange_code(platform, code, redirect_uri)`` swaps the code for
     ``{access_token, refresh_token, expires_in, user}``.
  4. ``put_user_token`` stores the token under
     ``eq-pdf:lti:user-token:{platform_id}:{user_id}`` (Redis TTL set to
     ``expires_in - buffer``; the refresh_token is persisted separately
     with a longer TTL so we can mint fresh access tokens silently).
  5. On 401 from the API, ``refresh_user_token`` exchanges the refresh
     token for a new pair.

Storage shape (JSON in Redis under the platform+user key)::

    {
      "access_token": "...",
      "refresh_token": "...",
      "expires_at": <unix seconds>,
      "canvas_user_id": "...",
      "canvas_user_name": "...",
      "obtained_at": "<iso>",
    }
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from ..canvas.tenant import tk
from ..lti.platform import PlatformInstall

logger = logging.getLogger(__name__)

# Per-user token cache. Key includes platform_id because two schools
# might have the same Canvas user-id and we must not cross the streams.
USER_TOKEN_KEY = tk("lti:user-token:{platform_id}:{user_id}")

# CSRF state for the OAuth2 redirect handshake. We store a short-lived
# state token mapped to the original LTI session so the callback can
# resume the journey to the review UI after consent.
OAUTH_STATE_KEY = tk("lti:oauth-state:{state}")

_EXPIRY_BUFFER_SECONDS = 60
_STATE_TTL_SECONDS = 600  # 10 min for the user to complete consent
_HTTP_TIMEOUT_SECONDS = 10.0


class UserOAuthError(Exception):
    """Anything went wrong during the user-OAuth flow."""


@dataclass
class UserToken:
    access_token: str
    refresh_token: str | None
    expires_at: float
    canvas_user_id: str
    canvas_user_name: str | None = None
    obtained_at: str = ""

    @classmethod
    def from_canvas_response(
        cls, payload: dict[str, Any], *, canvas_user_id: str | None = None
    ) -> UserToken:
        access = payload.get("access_token")
        if not access:
            raise UserOAuthError("token response missing access_token")
        # Refresh tokens may be absent on a token refresh (Canvas reuses
        # the long-lived one); the caller is responsible for preserving
        # the existing refresh_token in that case.
        refresh = payload.get("refresh_token")
        expires_in = float(payload.get("expires_in", 3600))
        # Canvas embeds the user identity in `user.id` on the first
        # code-exchange response. On subsequent refreshes the field is
        # absent, hence the optional override.
        user_obj = payload.get("user") or {}
        uid = canvas_user_id or str(user_obj.get("id") or "")
        return cls(
            access_token=str(access),
            refresh_token=str(refresh) if refresh else None,
            expires_at=time.time() + expires_in - _EXPIRY_BUFFER_SECONDS,
            canvas_user_id=uid,
            canvas_user_name=user_obj.get("name") or None,
            obtained_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OAuthState:
    """CSRF state stored between authorize-redirect and callback."""

    platform_id: str
    session_id: str
    return_url: str
    # When True the authorize flow was opened in a popup window; the
    # callback then returns a self-closing page that messages the opener
    # instead of redirecting. Defaulted so older stashed states (written
    # before this field existed) still deserialize via ``OAuthState(**...)``.
    popup: bool = False


def authorization_url(
    platform: PlatformInstall,
    *,
    redirect_uri: str,
    state: str,
    scopes: list[str] | None = None,
) -> str:
    """Build the ``/login/oauth2/auth`` URL we redirect the user to.

    The user lands on a Canvas-hosted consent page where they approve
    the tool's access. On approve, Canvas redirects them back to
    ``redirect_uri?code=...&state=...``.

    **Hostname split-brain alert.** Canvas Cloud uses TWO different
    hosts for the two halves of the OAuth dance:

      * The user-facing **authorize page** lives on the institution's
        host (e.g. ``csueb.test.instructure.com``) because that's
        where the user is logged in and where their Canvas account +
        role permissions live. The SSO host
        (``canvas.test.instructure.com``) does not know the user's
        per-institution permissions, so consent requests sent there
        fail with "exceeds scope granted by the resource owner".
      * The **token exchange endpoint** lives on the SSO host
        (``canvas.test.instructure.com/login/oauth2/token``). Token
        mints, client_credentials assertions, and refresh-grant calls
        all go there.

    So this function builds the URL from ``canvas_api_base`` (the
    institutional host), while ``exchange_code`` and
    ``refresh_user_token`` POST to ``auth_token_url`` (the SSO host).
    Both are correct and Canvas's docs confirm this asymmetry,
    confusingly.

    Scopes are space-separated per RFC 6749 -- not the LTI Advantage
    JSON-array style.
    """
    # Strip the /api/v1 suffix from canvas_api_base to get the bare
    # institutional host, then append the /login/oauth2/auth path.
    base = (platform.canvas_api_base or "").rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    # Prefer the OAuth API-key client_id when one is configured. Canvas
    # Cloud's /login/oauth2/auth endpoint only accepts non-LTI Developer
    # Keys; the platform's LTI client_id is rejected. Operators register
    # a sibling "API Key" in Canvas and set its client_id via the
    # CANVAS_OAUTH_CLIENT_ID env var.
    from ..config import settings as _s
    oauth_cid = (getattr(_s, "canvas_oauth_client_id", "") or "").strip()
    effective_client_id = oauth_cid or platform.client_id
    params: dict[str, str] = {
        "client_id": effective_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{base}/login/oauth2/auth?{urlencode(params)}"


async def exchange_code(
    platform: PlatformInstall,
    *,
    code: str,
    redirect_uri: str,
    client_assertion: str | None = None,
    client_secret: str | None = None,
) -> UserToken:
    """Swap an authorization code for a user-bound access+refresh pair.

    Canvas supports two client-authentication modes for the code
    exchange:

      * **JWT bearer assertion** -- pass ``client_assertion`` built by
        signing a JWT with our LTI private key. Same scheme as the
        client_credentials flow uses. Preferred when the dev key was
        registered with a public JWK URL (which is our case).
      * **client_secret** -- a shared secret that Canvas generates per
        dev key. Required for dev keys without a JWK; passed as a form
        field. Supported as a fallback.

    Exactly one of ``client_assertion`` or ``client_secret`` must be
    provided.
    """
    if bool(client_assertion) == bool(client_secret):
        raise UserOAuthError(
            "exchange_code: exactly one of client_assertion or client_secret required"
        )

    from ..config import settings as _s
    oauth_cid = (getattr(_s, "canvas_oauth_client_id", "") or "").strip()
    effective_client_id = oauth_cid or platform.client_id
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": effective_client_id,
    }
    # If the OAuth dev key requires a client_secret and one is configured
    # in env, use it; falls through to the assertion path otherwise.
    if not client_secret and not client_assertion:
        env_secret = getattr(_s, "canvas_oauth_client_secret", None)
        if env_secret is not None:
            sv = env_secret.get_secret_value() if hasattr(env_secret, "get_secret_value") else str(env_secret)
            if sv:
                client_secret = sv
    if client_assertion:
        data["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        data["client_assertion"] = client_assertion
    else:
        assert client_secret is not None
        data["client_secret"] = client_secret

    return await _post_token_endpoint(platform, data)


async def refresh_user_token(
    platform: PlatformInstall,
    *,
    refresh_token: str,
    canvas_user_id: str,
    client_assertion: str | None = None,
    client_secret: str | None = None,
) -> UserToken:
    """Use a stored refresh_token to mint a fresh access_token.

    Canvas reuses the same refresh_token across refreshes; we preserve
    the caller's existing refresh_token on the returned ``UserToken``
    if Canvas omits one in the response.
    """
    if bool(client_assertion) == bool(client_secret):
        raise UserOAuthError(
            "refresh_user_token: exactly one of client_assertion or "
            "client_secret required"
        )

    from ..config import settings as _s
    oauth_cid = (getattr(_s, "canvas_oauth_client_id", "") or "").strip()
    effective_client_id = oauth_cid or platform.client_id
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": effective_client_id,
    }
    if not client_secret and not client_assertion:
        env_secret = getattr(_s, "canvas_oauth_client_secret", None)
        if env_secret is not None:
            sv = env_secret.get_secret_value() if hasattr(env_secret, "get_secret_value") else str(env_secret)
            if sv:
                client_secret = sv
    if client_assertion:
        data["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        data["client_assertion"] = client_assertion
    else:
        assert client_secret is not None
        data["client_secret"] = client_secret

    fresh = await _post_token_endpoint(
        platform, data, canvas_user_id=canvas_user_id
    )
    # Canvas omits the refresh_token on refresh; preserve the existing one.
    if not fresh.refresh_token:
        fresh.refresh_token = refresh_token
    return fresh


async def _post_token_endpoint(
    platform: PlatformInstall,
    data: dict[str, str],
    *,
    canvas_user_id: str | None = None,
) -> UserToken:
    # Canvas Cloud's user-OAuth tokens are issued by the INSTITUTIONAL
    # host (e.g. csueb.instructure.com/login/oauth2/token), not the SSO
    # host stored in platform.auth_token_url. Auth codes are scoped to
    # the issuing tenant: a code from csueb.instructure.com/login/oauth2/auth
    # can only be redeemed at csueb.instructure.com/login/oauth2/token,
    # NOT at canvas.instructure.com (Free-for-Teacher host) or sso.canvaslms.com.
    # Derive the token URL from canvas_api_base so it always matches the
    # host that issued the auth code. Falls back to platform.auth_token_url
    # for self-hosted Canvases where canvas_api_base isn't set.
    user_token_url = platform.auth_token_url
    if platform.canvas_api_base:
        base = platform.canvas_api_base.rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        if base:
            user_token_url = f"{base}/login/oauth2/token"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                user_token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise UserOAuthError(f"token endpoint network error: {exc}") from exc

    if resp.status_code >= 400:
        body_preview = resp.text[:300]
        try:
            j = resp.json()
            body_preview = (
                f"error={j.get('error')!r} "
                f"description={j.get('error_description')!r}"
            )
        except ValueError:
            pass
        raise UserOAuthError(
            f"token endpoint HTTP {resp.status_code}: {body_preview}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UserOAuthError(f"token endpoint non-JSON response: {exc}") from exc

    token = UserToken.from_canvas_response(payload, canvas_user_id=canvas_user_id)
    # Emit an audit row. We don't know from inside _post_token_endpoint
    # whether this was a code-exchange or refresh, so use a generic event;
    # callers can add their own context above.
    from . import audit
    audit.emit(
        "user_token_grant",
        platform=platform.platform_id,
        user=token.canvas_user_id or canvas_user_id,
        ttl_s=int(token.expires_at - time.time()),
        had_refresh=bool(token.refresh_token),
    )
    return token


# ---------------------------------------------------------------------------
# Redis storage.
# ---------------------------------------------------------------------------


async def put_user_token(
    redis, platform_id: str, user_id: str, token: UserToken
) -> None:
    """Persist a user-OAuth token. Refresh_token persists at the same key.

    Storage is JSON with the access_token + refresh_token fields
    individually encrypted via ``privacy.encrypt_secret`` so a Redis
    snapshot leak cannot directly impersonate users. The remaining
    fields (canvas_user_id, name, expiry) are not secrets and stay
    plain to keep ops scripts useful.

    The Redis TTL covers the access_token's lifetime; we keep the
    JSON keyed past expiry via a separate longer-TTL store so that
    ``get_user_token`` can return a stale-access record whose
    refresh_token a caller can use to mint a fresh one. The longer
    TTL is 90 days, which matches Canvas's typical refresh-token
    longevity.
    """
    from .privacy import encrypt_secret
    payload = token.to_json()
    if payload.get("access_token"):
        payload["access_token"] = encrypt_secret(payload["access_token"])
    if payload.get("refresh_token"):
        payload["refresh_token"] = encrypt_secret(payload["refresh_token"])
    # Mark the record so ``get_user_token`` knows to decrypt.
    payload["_encrypted"] = True
    key = USER_TOKEN_KEY.format(platform_id=platform_id, user_id=user_id)
    body = json.dumps(payload)
    # 90 days for the record; the access_token expiry is encoded inside.
    await redis.set(key, body, ex=90 * 24 * 3600)


async def get_user_token(
    redis, platform_id: str, user_id: str
) -> UserToken | None:
    """Return the stored token for (platform, user), or None.

    Caller should check ``token.is_expired()`` and refresh if so.
    Legacy plaintext records (pre-encryption) are read through
    unchanged so the rollout doesn't require a migration.
    """
    key = USER_TOKEN_KEY.format(platform_id=platform_id, user_id=user_id)
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if data.pop("_encrypted", False):
            from .privacy import decrypt_secret
            if data.get("access_token"):
                data["access_token"] = decrypt_secret(data["access_token"])
            if data.get("refresh_token"):
                data["refresh_token"] = decrypt_secret(data["refresh_token"])
        return UserToken(**data)
    except (ValueError, TypeError):
        return None
    except RuntimeError as exc:
        logger.error("get_user_token: decrypt failed: %s", exc)
        return None


async def drop_user_token(redis, platform_id: str, user_id: str) -> None:
    """Delete a stored token. Called when refresh fails permanently."""
    key = USER_TOKEN_KEY.format(platform_id=platform_id, user_id=user_id)
    await redis.delete(key)


# ---------------------------------------------------------------------------
# OAuth state (CSRF protection for the redirect handshake).
# ---------------------------------------------------------------------------


def new_oauth_state_token() -> str:
    """Return a fresh random state value the callback will validate."""
    return secrets.token_urlsafe(32)


async def put_oauth_state(redis, state: str, payload: OAuthState) -> None:
    await redis.set(
        OAUTH_STATE_KEY.format(state=state),
        json.dumps(asdict(payload)),
        ex=_STATE_TTL_SECONDS,
    )


async def take_oauth_state(redis, state: str) -> OAuthState | None:
    """Single-use read: returns the state and deletes the record."""
    key = OAUTH_STATE_KEY.format(state=state)
    raw = await redis.get(key)
    if raw is None:
        return None
    await redis.delete(key)
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return OAuthState(**json.loads(raw))
    except (ValueError, TypeError):
        return None
