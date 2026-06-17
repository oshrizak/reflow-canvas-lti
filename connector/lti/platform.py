"""Per-tenant Canvas platform registry.

A ``PlatformInstall`` record captures everything Reflow needs to talk to
one specific Canvas instance: how to authenticate (token endpoints,
client_id, deployment_id), how to call its API (base URL), and metadata
useful for ops dashboards and audit logs.

One record per unique ``(issuer, client_id, deployment_id)`` triple.
Records are created on the first LTI launch from a new Canvas instance
and updated on every subsequent launch. The Phase 1 implementation only
*writes* these records - nothing reads them yet, so behaviour is
unchanged. Phase 2 (service-token client) is the first reader.

The composite key gets hashed to a short fixed-width id so we can use
it as a Redis key segment without worrying about issuer URLs containing
characters that confuse Redis tooling or log scrubbers.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


# Canonical Canvas URL conventions. Every Instructure-hosted and
# self-hosted Canvas exposes these paths under its main hostname, so we
# can derive them deterministically from just the issuer claim. The two
# exceptions are:
#   * Instructure's SSO issuer ``https://sso.canvaslms.com`` (which is
#     not the API host) - handled via the ``_SSO_HOST_OVERRIDE`` map.
#   * Self-hosted Canvases with non-standard path mounts - those need an
#     entry in platform_overrides.yaml at deploy time.
_CANVAS_TOKEN_PATH = "/login/oauth2/token"
_CANVAS_AUTH_LOGIN_PATH = "/api/lti/authorize_redirect"
_CANVAS_JWKS_PATH = "/api/lti/security/jwks"
_CANVAS_API_PATH = "/api/v1"

# Map known SSO-only issuer hostnames to the host that actually serves
# the Canvas API. Canvas's beta/test/production trios all share an SSO
# hostname while keeping per-environment API hosts.
_SSO_HOST_OVERRIDE = {
    "sso.canvaslms.com": "canvas.instructure.com",
    "sso.test.canvaslms.com": "canvas.test.instructure.com",
    "sso.beta.canvaslms.com": "canvas.beta.instructure.com",
}


@dataclass
class PlatformEndpoints:
    """Resolved auth + API URLs for one Canvas instance."""

    auth_token_url: str
    auth_login_url: str
    jwks_url: str
    canvas_api_base: str
    canvas_domain: str


@dataclass
class PlatformInstall:
    """One Canvas instance that has launched our tool.

    Identity is ``(issuer, client_id, deployment_id)``. Two installs of
    the same tool at the same Canvas instance (e.g. one at the
    sub-account level for the College of Engineering, another at the
    account level for everyone else) get distinct ``deployment_id``s and
    therefore distinct records.

    ``platform_id`` is a sha256 hash of the identity triple, truncated
    to 16 hex chars (64 bits of entropy is plenty for collision
    avoidance across the few-thousand-school deployment we are
    realistically targeting).
    """

    # Identity.
    issuer: str
    client_id: str
    deployment_id: str

    # Resolved endpoints.
    auth_token_url: str
    auth_login_url: str
    jwks_url: str
    canvas_api_base: str
    canvas_domain: str

    # Optional metadata. ``label`` is human-friendly text for ops
    # dashboards; ``granted_scopes`` records what the platform actually
    # approved (populated on first service-token request, Phase 2).
    label: str | None = None
    granted_scopes: list[str] = field(default_factory=list)

    # Audit. ISO 8601 strings (not datetimes) so the dataclass survives
    # round-tripping through json.dumps/loads without custom encoders.
    first_seen_at: str = ""
    last_launch_at: str = ""

    # Soft-revocation flag for incident response. When set, the launch
    # handler rejects new launches but in-flight jobs are not torn down.
    revoked_at: str | None = None

    @property
    def platform_id(self) -> str:
        return compute_platform_id(self.issuer, self.client_id, self.deployment_id)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PlatformInstall:
        # Filter unknown keys so a forward-compatible writer (later
        # phase adds a new field) does not crash the current reader.
        known = {f for f in cls.__dataclass_fields__}  # noqa: F841 - clarity
        kwargs = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**kwargs)


def compute_platform_id(issuer: str, client_id: str, deployment_id: str) -> str:
    """Stable hash of the identity triple, 16 hex chars."""
    blob = f"{issuer}|{client_id}|{deployment_id}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def derive_endpoints_from_issuer(issuer: str) -> PlatformEndpoints:
    """Build the Canvas auth + API URLs from a bare issuer claim.

    Most Canvas instances (Instructure-hosted and self-hosted alike)
    follow the same URL conventions, so a successful launch is enough
    information to call back. The exception is Instructure's shared SSO
    issuer hostnames, which front multiple regional API hosts; those
    are remapped via ``_SSO_HOST_OVERRIDE``.

    Raises ``ValueError`` if the issuer is unparseable. The caller is
    responsible for guarding against this; in practice the LTI launch
    handler has already validated the issuer matches the configured
    expected value before this is called.
    """

    parsed = urlparse(issuer.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Cannot derive Canvas endpoints: malformed issuer {issuer!r}")

    api_host = _SSO_HOST_OVERRIDE.get(parsed.netloc, parsed.netloc)
    base = f"{parsed.scheme}://{api_host}"

    # The token endpoint is hosted by the SSO domain itself when there
    # is one; Canvas validates the JWT bearer assertion at the SSO host
    # and returns a token usable against the API host. Issuers that
    # don't have a separate SSO host serve the token endpoint from the
    # same origin as the API.
    token_host = parsed.netloc
    token_base = f"{parsed.scheme}://{token_host}"

    return PlatformEndpoints(
        auth_token_url=f"{token_base}{_CANVAS_TOKEN_PATH}",
        auth_login_url=f"{token_base}{_CANVAS_AUTH_LOGIN_PATH}",
        jwks_url=f"{token_base}{_CANVAS_JWKS_PATH}",
        canvas_api_base=f"{base}{_CANVAS_API_PATH}",
        canvas_domain=api_host,
    )


def now_iso() -> str:
    """Timestamp string for the audit fields. UTC, no microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_install_from_launch(
    *,
    issuer: str,
    client_id: str,
    deployment_id: str,
    label: str | None = None,
) -> PlatformInstall:
    """Construct a fresh ``PlatformInstall`` from launch claims.

    ``first_seen_at`` and ``last_launch_at`` are both set to now; the
    store layer overwrites ``first_seen_at`` with the existing value
    if a record already exists for this identity.
    """

    endpoints = derive_endpoints_from_issuer(issuer)
    ts = now_iso()
    return PlatformInstall(
        issuer=issuer,
        client_id=client_id,
        deployment_id=deployment_id,
        auth_token_url=endpoints.auth_token_url,
        auth_login_url=endpoints.auth_login_url,
        jwks_url=endpoints.jwks_url,
        canvas_api_base=endpoints.canvas_api_base,
        canvas_domain=endpoints.canvas_domain,
        label=label,
        granted_scopes=[],
        first_seen_at=ts,
        last_launch_at=ts,
        revoked_at=None,
    )
