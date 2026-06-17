"""Structured audit log for Canvas API operations.

Every Canvas API call, every token mint/refresh, and every state
transition that affects a faculty member\'s data should write a row
here. Goals:

  * Production ops can answer "what did Reflow do in our Canvas
    yesterday for user X" within minutes from data, not memory.
  * Privacy review can confirm we never reach into a course we
    weren\'t launched into.
  * If Canvas reports anomalous traffic from our client_id, we can
    show them our matching log.

Records are written via the standard ``logging`` module under the
``reflow.canvas.audit`` logger. They are JSON-encoded so a downstream
log shipper (Datadog, CloudWatch, etc.) can index them as structured
data. For local dev they\'re also human-readable in plain text logs.

Field conventions:

  * ``event``       short verb-ish identifier (``api_call``, ``token_mint``,
                    ``token_refresh``, ``oauth_consent``, ...)
  * ``platform``    16-char platform_id (sha256 truncated)
  * ``user``        Canvas user_id (LTI ``sub``) when known
  * ``course``      Canvas course_id when relevant
  * ``method``      HTTP verb for API calls
  * ``path``        URL path (no host, no query) for API calls
  * ``status``      HTTP status from upstream
  * ``latency_ms``  monotonic latency in ms
  * ``scope_hash``  short hash of the scope set, to correlate calls
                    using the same token without leaking the scope list
  * ``request_id``  middleware-issued correlation id

Sensitive data NEVER appears: no access tokens, no refresh tokens, no
JWT assertions, no email addresses, no file contents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from typing import Any

_audit = logging.getLogger("reflow.canvas.audit")


def _scope_fingerprint(scopes: list[str] | tuple[str, ...] | None) -> str:
    """Order-independent 12-char hash of a scope set."""
    if not scopes:
        return "none"
    blob = "\n".join(sorted(scopes)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def emit(event: str, **fields: Any) -> None:
    """Write one structured audit row.

    Wraps ``logging.info`` so every record carries the same JSON body
    shape. Callers pass any subset of the conventional fields.
    """
    payload: dict[str, Any] = {"event": event}
    for k, v in fields.items():
        if v is None:
            continue
        if k == "scopes":
            payload["scope_hash"] = _scope_fingerprint(v)
            continue
        payload[k] = v
    try:
        _audit.info(json.dumps(payload, default=str))
    except Exception:  # pragma: no cover -- audit must never raise
        _audit.info("audit-emit-failed event=%s", event)


@contextmanager
def time_api_call(
    *,
    platform: str | None,
    user: str | None,
    method: str,
    path: str,
    scopes: list[str] | None = None,
):
    """Context manager for an outbound Canvas API call.

    Use::

        with audit.time_api_call(platform=..., user=..., method="GET",
                                  path="/api/v1/courses/x/files") as ctx:
            resp = await http.get(...)
            ctx["status"] = resp.status_code

    On exit emits a single ``api_call`` row with status + latency. If an
    exception escapes, ``status`` is set to ``-1`` so ops can find
    request-aborted calls (DNS timeouts, etc.) separately from upstream
    4xx/5xx.
    """
    ctx: dict[str, Any] = {"status": None}
    started = time.monotonic()
    try:
        yield ctx
    except Exception:
        ctx["status"] = -1
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        emit(
            "api_call",
            platform=platform,
            user=user,
            method=method,
            path=path,
            status=ctx.get("status"),
            latency_ms=latency_ms,
            scopes=scopes,
        )
