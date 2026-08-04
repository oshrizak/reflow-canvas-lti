"""Validate configuration before the connector boots.

Run after filling in ``.env`` and before starting the stack:

    python scripts/preflight.py
    docker compose run --rm connector python scripts/preflight.py

Each check answers a question that otherwise gets answered later by a
confusing runtime failure. A missing key pair shows up as an LTI launch
that dies mid-handshake; a trailing slash on the public URL shows up as
Canvas rejecting the redirect URI as unregistered; an unset Reflow API key
shows up as documents that submit and then never convert.

Exits 0 when the connector should be able to start, 1 otherwise. Warnings
never fail the run — they mark settings that are legal but rarely what an
operator intended.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run by path (``python scripts/preflight.py``) sys.path[0] is scripts/, so
# the connector package is invisible. Put the repo root first — the whole
# point of this script is to be runnable before anything is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

errors: list[str] = []
warnings: list[str] = []


def _secret(value: object) -> str:
    """Read a pydantic ``SecretStr`` or a plain string uniformly."""
    if value is None:
        return ""
    get = getattr(value, "get_secret_value", None)
    return (get() if callable(get) else str(value)).strip()


def _text(settings: object, name: str) -> str:
    return (getattr(settings, name, "") or "").strip()


def main() -> int:
    try:
        from connector.config import settings
    except Exception as exc:  # noqa: BLE001 — any import failure is fatal here
        print(f"FAIL: configuration could not be loaded: {exc}", file=sys.stderr)
        print("      Check .env against .env.example.", file=sys.stderr)
        return 1

    # --- Reflow Core ----------------------------------------------------
    if not _text(settings, "reflow_api_base_url"):
        errors.append(
            "REFLOW_API_BASE_URL is not set — the connector has no Reflow Core "
            "to convert with."
        )
    if not _secret(getattr(settings, "reflow_api_key", None)):
        errors.append(
            "REFLOW_API_KEY is not set — document submission will be rejected."
        )

    if not getattr(settings, "lti_enabled", False):
        warnings.append(
            "LTI_ENABLED is false. The API will start, but Canvas launches and "
            "the watcher/bridge workers stay switched off."
        )
        _report()
        return 1 if errors else 0

    # --- LTI identity ---------------------------------------------------
    public_url = _text(settings, "lti_public_url")
    if not public_url:
        errors.append(
            "LTI_PUBLIC_URL is not set. Canvas needs an absolute, publicly "
            "reachable URL for the launch and OAuth callbacks."
        )
    else:
        if not public_url.startswith("https://"):
            warnings.append(
                f"LTI_PUBLIC_URL is {public_url!r}. Canvas requires https for "
                "any real install; http only works for local experiments."
            )
        if public_url.endswith("/"):
            warnings.append(
                "LTI_PUBLIC_URL has a trailing slash. Redirect URIs are built by "
                "concatenation, producing '//canvas/oauth/callback', which Canvas "
                "rejects as an unregistered redirect URI."
            )

    for name in (
        "lti_issuer",
        "lti_client_id",
        "lti_deployment_id",
        "lti_auth_login_url",
        "lti_auth_token_url",
        "lti_jwks_url",
    ):
        value = _text(settings, name)
        if not value:
            errors.append(f"{name.upper()} is not set — LTI launches cannot be validated.")
        elif value.upper().startswith("FILL-IN"):
            errors.append(f"{name.upper()} still holds a placeholder value ({value!r}).")

    # --- Key pair --------------------------------------------------------
    # The most common first-run failure, and the least obvious from the
    # error it eventually produces.
    private = Path(_text(settings, "lti_private_key_path") or "keys/private.pem")
    if not private.exists():
        errors.append(
            f"No LTI private key at {private}. Generate one with "
            "scripts/generate_lti_keys.sh — Canvas cannot verify the tool without it."
        )

    # --- Canvas OAuth ----------------------------------------------------
    # Optional for boot, but nothing publishes without it: the watcher and
    # bridge act on a user's behalf, not the tool's.
    oauth_id = _text(settings, "canvas_oauth_client_id")
    oauth_secret = _secret(getattr(settings, "canvas_oauth_client_secret", None))
    if bool(oauth_id) != bool(oauth_secret):
        errors.append(
            "CANVAS_OAUTH_CLIENT_ID and CANVAS_OAUTH_CLIENT_SECRET must be set "
            "together — one without the other cannot complete the OAuth flow."
        )
    elif not oauth_id:
        warnings.append(
            "Canvas OAuth is not configured. Launches will work, but no document "
            "will ever be published: publishing acts as a user, not as the tool."
        )

    _report()
    return 1 if errors else 0


def _report() -> None:
    for msg in warnings:
        print(f"WARN: {msg}")
    for msg in errors:
        print(f"FAIL: {msg}", file=sys.stderr)
    if errors:
        print(
            f"\npreflight: {len(errors)} problem(s) must be fixed before boot.",
            file=sys.stderr,
        )
    else:
        suffix = f" ({len(warnings)} warning(s))" if warnings else ""
        print(f"preflight: configuration looks usable{suffix}.")


if __name__ == "__main__":
    raise SystemExit(main())
