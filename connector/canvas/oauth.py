"""LTI Advantage client-credentials service tokens for Canvas.

Phase 2 of the multi-tenant migration. Given a ``PlatformInstall`` and a
list of scopes, this module returns a short-lived bearer token that
authenticates the tool (not any specific user) against that platform's
Canvas API.

Flow per RFC 7521 + RFC 7523:

  1. Build a JWT with ``iss == sub == our client_id at this platform``,
     ``aud == platform.auth_token_url``, plus iat/exp/jti.
  2. Sign with our LTI private key (the same one that signs outgoing LTI
     Advantage assertions and is published in ``/lti/jwks``).
  3. POST to the platform's token endpoint with ``grant_type=
     client_credentials``, ``client_assertion_type=...:jwt-bearer``,
     ``client_assertion=<the JWT>``, and ``scope=<space-separated>``.
  4. Cache the returned bearer in Redis keyed by ``(platform_id,
     scope_hash)`` with TTL = ``expires_in - 30s`` (30s buffer so we
     never hand out a token that expires mid-request).

Cache invalidation: the caller (``CanvasClient`` in Phase 3) is
responsible for calling ``invalidate`` on a 401 and retrying once.
This module does not retry on its own because the right retry policy
depends on what the caller was doing (read vs. write).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jwcrypto import jwk, jwt

from ..canvas.tenant import tk
from ..lti.keys import load_private_key
from ..lti.platform import PlatformInstall

logger = logging.getLogger(__name__)

# Token cache. Phase 1's platform records live at lti:platform:* so
# this sibling namespace keeps service-token state visibly distinct.
TOKEN_KEY = tk("lti:service-token:{platform_id}:{scope_hash}")

# Expiry buffer. Canvas tokens are typically 1 hour; subtracting 30s
# guarantees we hand back a token that survives the call we are about
# to make even on a slow request path.
_EXPIRY_BUFFER_SECONDS = 30

# JWT bearer assertion lifetime. 5 minutes is what the IMS LTI 1.3
# Security Framework recommends — short enough that a replay window is
# bounded, long enough to survive clock skew between us and the platform.
_ASSERTION_TTL_SECONDS = 300

# HTTP timeout for the token exchange. The platform's token endpoint
# normally responds in well under a second; 10s covers cold caches and
# pathological cases without stalling a worker forever.
_HTTP_TIMEOUT_SECONDS = 10.0


class ServiceTokenError(Exception):
    """Raised when we cannot mint or refresh a service token.

    Carries the platform identity and a sanitized reason. Never includes
    the assertion JWT or the bearer itself — those are sensitive and we
    do not want them in stack traces or error pages.
    """

    def __init__(self, platform_id: str, reason: str) -> None:
        super().__init__(f"service token failed for platform={platform_id}: {reason}")
        self.platform_id = platform_id
        self.reason = reason


@dataclass
class CachedToken:
    """A bearer token plus the absolute unix timestamp it stops being valid."""

    access_token: str
    granted_scope: str
    expires_at: float


def _scope_hash(scopes: list[str] | tuple[str, ...]) -> str:
    """Stable hash of a scope set, order-independent."""
    if not scopes:
        return "none"
    blob = "\n".join(sorted(scopes)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _our_jwk(private_key) -> jwk.JWK:
    """Build a jwcrypto JWK from a cryptography RSAPrivateKey.

    We use jwcrypto's PEM importer to match what ``jwks_document`` does
    so the ``kid`` thumbprint stays consistent between assertion-signing
    and JWKS publication. Canvas uses the ``kid`` claim header to pick
    the right key out of our JWKS.
    """
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key = jwk.JWK.from_pem(pem)
    return key


def _service_token_url(platform: PlatformInstall) -> str:
    """Canonical Canvas Cloud token endpoint for the client_credentials grant.

    Canvas Cloud issues LTI-Advantage service tokens from a shared SSO host
    per environment (``sso[.test|.beta].canvaslms.com``), NOT the regional
    ``*.instructure.com`` host used for launches and API calls -- even vanity
    domains (e.g. ``csueb.test.instructure.com``) must go through the SSO
    host. The JWT bearer assertion's ``aud`` must equal this URL and the POST
    must go to it, or Canvas rejects with ``error='invalid_request'
    description="the 'aud' is invalid"``. Self-hosted Canvas co-locates the
    token endpoint with the issuer, so we fall back to the configured one.
    """
    from urllib.parse import urlparse

    parsed = urlparse((platform.issuer or "").strip())
    host = (parsed.netloc or "").lower()
    scheme = parsed.scheme or "https"
    sso: str | None = None
    if host in {"sso.canvaslms.com", "sso.test.canvaslms.com", "sso.beta.canvaslms.com"}:
        sso = host
    elif host.endswith(".test.instructure.com") or host == "test.instructure.com":
        sso = "sso.test.canvaslms.com"
    elif host.endswith(".beta.instructure.com") or host == "beta.instructure.com":
        sso = "sso.beta.canvaslms.com"
    elif host.endswith(".instructure.com") or host == "instructure.com":
        sso = "sso.canvaslms.com"
    if sso:
        return f"{scheme}://{sso}/login/oauth2/token"
    # Self-hosted / unknown Canvas: trust the endpoint resolved at launch.
    return platform.auth_token_url


def _build_assertion(platform: PlatformInstall) -> str:
    """Build and sign the JWT bearer client-authentication assertion."""

    private_key = load_private_key()
    key = _our_jwk(private_key)
    kid = key.thumbprint()

    now = int(time.time())
    # Canvas Cloud validates the JWT-bearer assertion's ``aud`` against its
    # canonical SSO token endpoint (sso[.test|.beta].canvaslms.com), which is
    # where we also POST. ``_service_token_url`` maps the issuer to that host.
    audience = _service_token_url(platform)
    claims: dict[str, Any] = {
        "iss": platform.client_id,
        "sub": platform.client_id,
        "aud": audience,
        "iat": now,
        "exp": now + _ASSERTION_TTL_SECONDS,
        # jti must be unique per assertion; the platform may track
        # recent jtis to defend against replay. token_urlsafe(32) gives
        # 256 bits of entropy which is plenty.
        "jti": secrets.token_urlsafe(32),
    }

    token = jwt.JWT(
        header={"alg": "RS256", "typ": "JWT", "kid": kid},
        claims=claims,
    )
    token.make_signed_token(key)
    return token.serialize()


async def _exchange_assertion(
    platform: PlatformInstall,
    assertion: str,
    scopes: list[str],
) -> CachedToken:
    """POST the assertion to the platform's token endpoint."""

    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "scope": " ".join(scopes),
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _service_token_url(platform),
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ServiceTokenError(
            platform.platform_id, f"network error contacting token endpoint: {exc}"
        ) from exc

    if resp.status_code >= 400:
        # The platform returned an error. The body usually contains
        # ``error`` and ``error_description`` per RFC 6749; log those
        # but do NOT log the assertion (which is in the request body).
        body_preview = ""
        try:
            body = resp.json()
            body_preview = (
                f"error={body.get('error')!r} "
                f"description={body.get('error_description')!r}"
            )
        except (json.JSONDecodeError, ValueError):
            body_preview = resp.text[:200]
        raise ServiceTokenError(
            platform.platform_id,
            f"token endpoint returned HTTP {resp.status_code}: {body_preview}",
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ServiceTokenError(
            platform.platform_id, f"token endpoint returned non-JSON: {exc}"
        ) from exc

    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not access_token or not isinstance(expires_in, (int, float)):
        raise ServiceTokenError(
            platform.platform_id,
            "token endpoint response missing access_token or expires_in",
        )

    # Canvas's actual granted scope set may differ from what we
    # requested (e.g. if some scopes were not approved). Record what we
    # actually got back so callers can decide whether to retry without
    # the missing scopes or surface a "tell your admin" message.
    granted_scope = payload.get("scope", "")

    return CachedToken(
        access_token=str(access_token),
        granted_scope=str(granted_scope),
        expires_at=time.time() + float(expires_in) - _EXPIRY_BUFFER_SECONDS,
    )


def _cache_key(platform_id: str, scopes: list[str]) -> str:
    return TOKEN_KEY.format(platform_id=platform_id, scope_hash=_scope_hash(scopes))


async def _load_cached(redis, platform_id: str, scopes: list[str]) -> CachedToken | None:
    raw = await redis.get(_cache_key(platform_id, scopes))
    if not raw:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        cached = CachedToken(
            access_token=data["access_token"],
            granted_scope=data.get("granted_scope", ""),
            expires_at=float(data["expires_at"]),
        )
    except (KeyError, ValueError, TypeError):
        return None
    if cached.expires_at <= time.time():
        return None
    return cached


async def _store_cached(redis, platform_id: str, scopes: list[str], cached: CachedToken) -> None:
    ttl = int(cached.expires_at - time.time())
    if ttl <= 0:
        return
    await redis.set(
        _cache_key(platform_id, scopes),
        json.dumps({
            "access_token": cached.access_token,
            "granted_scope": cached.granted_scope,
            "expires_at": cached.expires_at,
        }),
        ex=ttl,
    )


async def get_service_token(
    redis,
    platform: PlatformInstall,
    scopes: list[str],
    *,
    force_refresh: bool = False,
) -> CachedToken:
    """Return a service token good for the given scopes on this platform.

    Cached tokens are returned without a network round-trip when valid.
    Pass ``force_refresh=True`` to bypass the cache; this is the path a
    401-handling caller takes on retry.

    Caller is responsible for catching ``ServiceTokenError`` and
    rendering it appropriately (logs vs. user-facing error vs. retry).
    """

    if platform.revoked_at:
        raise ServiceTokenError(
            platform.platform_id,
            f"platform is soft-revoked since {platform.revoked_at}",
        )

    if not force_refresh:
        cached = await _load_cached(redis, platform.platform_id, scopes)
        if cached is not None:
            return cached

    assertion = _build_assertion(platform)
    fresh = await _exchange_assertion(platform, assertion, scopes)
    await _store_cached(redis, platform.platform_id, scopes, fresh)

    logger.info(
        "service token minted: platform_id=%s scope_hash=%s granted=%r ttl=%ds",
        platform.platform_id,
        _scope_hash(scopes),
        fresh.granted_scope[:80],
        int(fresh.expires_at - time.time()),
    )
    from . import audit
    audit.emit(
        "service_token_mint",
        platform=platform.platform_id,
        scopes=scopes,
        ttl_s=int(fresh.expires_at - time.time()),
        granted=fresh.granted_scope[:200],
    )
    return fresh


async def invalidate(redis, platform_id: str, scopes: list[str]) -> None:
    """Drop a cached service token for (platform, scope-set).

    Call this on a 401 from Canvas before retrying. The next call to
    ``get_service_token`` will mint a fresh one.
    """
    await redis.delete(_cache_key(platform_id, scopes))
