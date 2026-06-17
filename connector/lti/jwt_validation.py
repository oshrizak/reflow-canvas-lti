"""Validate Canvas-issued LTI 1.3 launch JWTs against the platform JWKS."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jwcrypto import jwk, jwt

from .config import get_lti_settings

logger = logging.getLogger(__name__)


class LtiValidationError(Exception):
    """Raised when an incoming LTI JWT fails validation."""


@dataclass
class LaunchClaims:
    """Normalised view of the claims we care about.

    The full JWT contains ~20 LTI claims; this dataclass exposes only the
    handful that downstream code actually needs. If you need a new one,
    add it here rather than threading raw dicts through the codebase.
    """

    issuer: str
    audience: str
    subject: str
    user_id: str
    user_name: str | None
    user_email: str | None
    roles: list[str]
    course_id: str
    course_title: str | None
    deployment_id: str
    target_link_uri: str
    raw: dict[str, Any]


_JWKS_CACHE: dict[str, tuple[float, jwk.JWKSet]] = {}
_JWKS_TTL_SECONDS = 600


async def _fetch_platform_jwks(jwks_url: str) -> jwk.JWKSet:
    cached = _JWKS_CACHE.get(jwks_url)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        keyset = jwk.JWKSet.from_json(resp.text)

    _JWKS_CACHE[jwks_url] = (now + _JWKS_TTL_SECONDS, keyset)
    return keyset


async def validate_launch_jwt(id_token: str) -> LaunchClaims:
    """Verify signature, issuer, audience, and required LTI claims.

    Returns a ``LaunchClaims`` dataclass on success. Raises
    ``LtiValidationError`` on any failure with a message safe to log
    (no token material).
    """

    cfg = get_lti_settings()
    if not cfg.enabled:
        raise LtiValidationError("LTI integration is disabled")

    try:
        keyset = await _fetch_platform_jwks(cfg.jwks_url)
    except httpx.HTTPError as exc:
        raise LtiValidationError(f"Could not fetch platform JWKS: {exc}") from exc

    try:
        token = jwt.JWT(jwt=id_token, key=keyset)
        claims = token.claims if isinstance(token.claims, dict) else _parse_json(token.claims)
    except Exception as exc:  # jwcrypto raises a tree of types; collapse for the caller
        raise LtiValidationError(f"JWT signature/decoding failed: {exc}") from exc

    _check_required(claims, cfg.issuer, cfg.client_id, cfg.deployment_id)

    return LaunchClaims(
        issuer=claims["iss"],
        audience=_audience_of(claims["aud"]),
        subject=claims["sub"],
        user_id=claims["sub"],
        user_name=claims.get("name"),
        user_email=claims.get("email"),
        roles=claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", []),
        # Prefer the numeric Canvas course ID from the custom claim — that's
        # what every other surface keys off (the watcher, scored_files, and
        # window.ENV.COURSE_ID in the Theme JS overlay). Canvas only sends
        # this if the tool config declares ``course_id: $Canvas.course.id``
        # under custom_fields (see register_tool_config in routes.py).
        # Fall back to context.id (the opaque LTI hash) so launches keep
        # working even on platforms that don't expand the substitution.
        course_id=str(
            claims.get("https://purl.imsglobal.org/spec/lti/claim/custom", {}).get("course_id")
            or claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {}).get("id", "")
        ),
        course_title=claims.get(
            "https://purl.imsglobal.org/spec/lti/claim/context", {}
        ).get("title"),
        deployment_id=claims["https://purl.imsglobal.org/spec/lti/claim/deployment_id"],
        target_link_uri=claims.get(
            "https://purl.imsglobal.org/spec/lti/claim/target_link_uri", ""
        ),
        raw=claims,
    )


def _parse_json(claims_str: str) -> dict[str, Any]:
    import json

    return json.loads(claims_str)


def _audience_of(aud: Any) -> str:
    if isinstance(aud, list):
        return aud[0] if aud else ""
    return str(aud)


def _check_required(
    claims: dict[str, Any],
    expected_issuer: str,
    expected_client_id: str,
    expected_deployment_id: str,
) -> None:
    if claims.get("iss") != expected_issuer:
        raise LtiValidationError("Unexpected issuer")

    audience = _audience_of(claims.get("aud"))
    if audience != expected_client_id:
        raise LtiValidationError("Unexpected audience")

    deployment_claim = claims.get(
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
    )
    if deployment_claim != expected_deployment_id:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "LTI deployment_id mismatch: got=%r expected=%r",
            deployment_claim, expected_deployment_id,
        )
        raise LtiValidationError("Unexpected deployment_id")

    message_type = claims.get(
        "https://purl.imsglobal.org/spec/lti/claim/message_type"
    )
    if message_type != "LtiResourceLinkRequest":
        raise LtiValidationError(f"Unsupported LTI message type: {message_type}")

    now = int(time.time())
    if "exp" in claims and now >= int(claims["exp"]):
        raise LtiValidationError("Token expired")
    if "iat" in claims and int(claims["iat"]) > now + 300:
        raise LtiValidationError("Token issued in the future")
