"""LTI-specific settings, pulled from the top-level ``Settings`` object.

Kept as a thin wrapper so the rest of the LTI package never has to import
the global settings module directly; tests can monkeypatch this view.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class LtiSettings:
    """Resolved LTI configuration.

    Every field is required when ``LTI_ENABLED`` is true. Defaults are kept
    permissive so the import does not blow up in dev when LTI is off.
    """

    enabled: bool
    issuer: str
    client_id: str
    deployment_id: str
    auth_login_url: str
    auth_token_url: str
    jwks_url: str
    private_key_path: str
    public_key_path: str
    canvas_api_url: str
    state_ttl_seconds: int


def get_lti_settings() -> LtiSettings:
    """Return resolved LTI settings.

    Reads the underlying ``Settings`` object on every call so tests that
    patch settings see the change without restarting the process.
    """

    s = settings
    return LtiSettings(
        enabled=bool(getattr(s, "lti_enabled", False)),
        issuer=getattr(s, "lti_issuer", "") or "",
        client_id=getattr(s, "lti_client_id", "") or "",
        deployment_id=getattr(s, "lti_deployment_id", "") or "",
        auth_login_url=getattr(s, "lti_auth_login_url", "") or "",
        auth_token_url=getattr(s, "lti_auth_token_url", "") or "",
        jwks_url=getattr(s, "lti_jwks_url", "") or "",
        private_key_path=getattr(s, "lti_private_key_path", "/app/keys/lti_private.pem"),
        public_key_path=getattr(s, "lti_public_key_path", "/app/keys/lti_public.pem"),
        canvas_api_url=getattr(s, "canvas_api_url", "") or "",
        state_ttl_seconds=int(getattr(s, "lti_state_ttl_seconds", 600)),
    )
