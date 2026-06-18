"""Connector configuration.

Reads from environment / ``.env`` via ``pydantic-settings``. This is a
SUBSET of upstream Reflow Core's Settings: only fields the connector's LTI
+ Canvas modules actually use, plus a new ``reflow_api_base_url`` pointing
at the upstream Reflow Core HTTP API.

Field naming intentionally mirrors the source fork's names so that ported
modules can keep ``settings.canvas_*`` / ``settings.lti_*`` access patterns
verbatim.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Connector settings from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ------------------------------------------------------------------
    # Reflow Core (upstream) HTTP API — the connector is a client of this
    # ------------------------------------------------------------------
    reflow_api_base_url: str = Field(
        default="http://localhost:8080",
        description=(
            "Base URL of the upstream Reflow Core API the connector talks to "
            "(document submit, status poll, PII approve/deny, presigned-URL fetch)."
        ),
    )
    reflow_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "API key the connector sends to Reflow Core. ``None`` disables outbound "
            "auth — only safe for local dev where Reflow Core has auth disabled."
        ),
    )
    reflow_poll_seconds: int = Field(
        ge=5,
        le=600,
        default=30,
        description="How often the bridge polls Reflow Core for job status.",
    )

    # ------------------------------------------------------------------
    # FastAPI runtime
    # ------------------------------------------------------------------
    api_host: str = Field(default="0.0.0.0", description="Host the FastAPI server binds to.")
    api_port: int = Field(
        ge=1, le=65535, default=8000,
        description="Port the FastAPI server listens on.",
    )
    log_level: str = Field(default="INFO", description="Log level (DEBUG, INFO, WARNING, ERROR).")
    environment: str = Field(default="production", description="Runtime label ('dev' or 'production').")

    # ------------------------------------------------------------------
    # API key auth — clients calling THIS connector
    # ------------------------------------------------------------------
    enable_api_key_auth: bool = Field(
        default=True, description="Require an API key on protected endpoints."
    )
    api_key_header_name: str = Field(
        default="X-API-Key", description="Header to read API keys from."
    )
    api_keys: SecretStr | None = Field(
        default=None,
        description=(
            "Comma-separated allowlist. Each entry is a bare key or a 'label:key' "
            "pair (split on the first colon). The label is logged on every "
            "authenticated request; the key itself is never logged."
        ),
    )

    # ------------------------------------------------------------------
    # Redis — Canvas-side state (``eq-pdf:lti:*``, ``eq-pdf:canvas:*``)
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis URL for Canvas-side state and LTI session storage.",
    )
    redis_max_connections: int = Field(
        ge=1, le=1000, default=10,
        description="Maximum connections in the Redis client pool.",
    )

    # ------------------------------------------------------------------
    # S3 — presigned URLs returned by Reflow Core
    # ------------------------------------------------------------------
    aws_region: str = Field(default="us-east-1", description="AWS region for boto3 clients.")
    s3_public_url: str | None = Field(
        default=None,
        description="Public S3 URL used in faculty-facing links.",
    )
    # The 2026-06-17 bug from the source fork: without this the bridge fails
    # to fetch markdown from Reflow Core's presigned URLs when the public host
    # isn't reachable from inside the connector container.
    s3_internal_url: str | None = Field(
        default=None,
        description=(
            "Internal S3 URL the connector rewrites presigned URLs to before "
            "fetching. Required when ``s3_public_url`` isn't reachable from the "
            "connector (e.g. presigned URL points at 'localhost' but the "
            "connector lives in Docker)."
        ),
    )

    # ------------------------------------------------------------------
    # LTI 1.3
    # ------------------------------------------------------------------
    lti_enabled: bool = Field(default=False, description="Enable LTI endpoints and Canvas workers.")
    lti_issuer: str | None = Field(
        default=None, description="Canvas issuer URL (e.g. https://canvas.instructure.com)."
    )
    lti_client_id: str | None = Field(default=None, description="Canvas Developer Key client_id.")
    lti_deployment_id: str | None = Field(default=None, description="Canvas deployment_id.")
    lti_auth_login_url: str | None = Field(
        default=None, description="Canvas OIDC authorize_redirect endpoint."
    )
    lti_auth_token_url: str | None = Field(
        default=None, description="Canvas OAuth2 token endpoint."
    )
    lti_jwks_url: str | None = Field(default=None, description="Canvas JWKS endpoint.")
    lti_private_key_path: str = Field(
        default="/app/keys/lti_private.pem", description="Path to LTI signing private key."
    )
    lti_public_key_path: str = Field(
        default="/app/keys/lti_public.pem", description="Path to LTI signing public key."
    )
    lti_state_ttl_seconds: int = Field(
        ge=60, le=3600, default=600, description="OIDC state nonce TTL (seconds)."
    )
    lti_public_url: str | None = Field(
        default=None,
        description="Public origin of this tool (used in LTI tool config and notification deep links).",
    )

    # ------------------------------------------------------------------
    # Canvas LMS
    # ------------------------------------------------------------------
    canvas_tenant: str = Field(
        default="default",
        description=(
            "Tenant id for Redis key namespacing. 'default' uses unprefixed keys; "
            "any other value namespaces every key as ``eq-pdf:t:{tenant}:...`` so "
            "multiple campuses can share one Redis."
        ),
    )
    canvas_api_url: str | None = Field(default=None, description="Canvas instance base URL.")
    canvas_api_token: SecretStr | None = Field(
        default=None,
        description="Canvas API token (fallback when not using per-instructor OAuth).",
    )
    canvas_watched_courses: str = Field(
        default="", description="Comma-separated Canvas course ids the watcher polls."
    )
    multi_tenant_watcher: bool = Field(
        default=False,
        description="Watcher iterates LTI-registered platforms instead of ``canvas_watched_courses``.",
    )
    canvas_oauth_client_id: str = Field(
        default="",
        description=(
            "Canvas API Key (non-LTI Developer Key) client_id for the user-OAuth2 "
            "flow. Required because LTI 1.3 keys are not accepted by Canvas's "
            "/login/oauth2/auth endpoint."
        ),
    )
    canvas_oauth_client_secret: SecretStr | None = Field(
        default=None,
        description="Secret for the user-OAuth API Key.",
    )
    canvas_allowed_origins: str = Field(
        default="",
        description="Comma-separated Canvas origins allowed to call panorama endpoints.",
    )
    canvas_allowed_origin_regex: str = Field(
        default="",
        description="Regex of Canvas origins allowed (takes precedence over ``canvas_allowed_origins``).",
    )
    canvas_poll_seconds: int = Field(
        ge=15, le=3600, default=60, description="Canvas file poll interval (seconds)."
    )

    # Per-course AI-API spend cap. Default of 0 disables the cap;
    # operators opt in by setting a per-course budget.
    canvas_monthly_spend_cap_usd_default: int = Field(
        ge=0, le=100_000, default=0,
        description="Default monthly AI-API spend cap in USD per Canvas course (0=unlimited).",
    )
    canvas_monthly_spend_cap_overrides: str = Field(
        default="",
        description='JSON map of course_id->cap_usd overrides, e.g. \'{"50594": 250}\'.',
    )
    canvas_estimated_cost_per_doc_cents: int = Field(
        ge=0, le=10000, default=10,
        description="Pre-flight per-document cost estimate in cents (used by the spend cap).",
    )

    # Canvas-side data retention. 0 disables purging.
    canvas_job_retention_days: int = Field(
        ge=0, le=3650, default=90,
        description="Days to retain Canvas job records in Redis (0=keep forever).",
    )
    canvas_audit_retention_days: int = Field(
        ge=0, le=3650, default=365,
        description="Days to retain approval-audit events in Redis (0=keep forever).",
    )
    canvas_retention_sweep_every_ticks: int = Field(
        ge=1, le=10080, default=240,
        description="Run the retention sweep every N watcher ticks (~4h at the 60s default poll).",
    )

    # ------------------------------------------------------------------
    # Prometheus metrics
    # ------------------------------------------------------------------
    enable_metrics: bool = Field(
        default=True, description="Enable Prometheus metrics collection and the /metrics endpoint."
    )
    metrics_port: int = Field(
        ge=1, le=65535, default=8001,
        description="Port for the Prometheus metrics server.",
    )


settings = Settings()
