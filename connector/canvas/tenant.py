"""Tenant + per-platform namespacing for Redis keys.

Two layers of isolation live here:

  * **Deployment tenant** (legacy): one Reflow install can carve up its
    Redis under ``CANVAS_TENANT``. When unset (or ``default``), the
    legacy ``eq-pdf:`` prefix is used. When set, every key lives under
    ``eq-pdf:t:<tenant>:``. This is a process-startup decision.

  * **LTI platform** (Phase 7): one deployment can host data for many
    Canvas instances. Calls that pass ``platform=`` get a key under
    ``eq-pdf:p:<platform_id>:`` (inside whatever deployment tenant the
    process is configured for). Calls that don't pass it land at the
    deployment-level namespace, same as before.

Together, the full key shape is::

    eq-pdf[:t:<tenant>][:p:<platform_id>]:<suffix>

with both middle segments optional. The legacy ``tk(suffix)`` call
behaves exactly as it always did; the new ``tk(suffix, platform=...)``
adds the per-platform sandbox without disturbing anything else.

Why this lives in ``canvas/`` and not ``lti/``: the helper is used by
every storage callsite, most of which predate the LTI work. Keeping it
co-located with the rest of the Canvas-state-shaped code means the
import graph stays shallow (storage modules don't have to import
``lti.platform`` just to compute a key).
"""

from __future__ import annotations

import re
from typing import Any

from ..config import settings

# Tenant ids are user-facing in the sense that they appear in log lines
# and Redis key names. Keep them URL-safe and short so operators can
# eyeball Redis output during debugging without surprise.
_TENANT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def _resolve_prefix() -> str:
    raw = (getattr(settings, "canvas_tenant", "") or "").strip().lower()
    if not raw or raw == "default":
        return "eq-pdf"
    if not _TENANT_RE.match(raw):
        raise ValueError(
            f"Invalid canvas_tenant {raw!r}: must match [a-z0-9_-]{{1,40}}"
        )
    return f"eq-pdf:t:{raw}"


# Cache once at import time. Tenant changes require a process restart -
# this is consistent with how operators treat the value.
_PREFIX = _resolve_prefix()


def _platform_id_of(platform: Any) -> str:
    """Coerce a platform argument into a short id string.

    Accepts either a ``PlatformInstall`` (with ``.platform_id``) or a
    bare string. Other shapes raise to surface a mistake at the
    callsite rather than silently producing a malformed key.
    """
    if platform is None:
        return ""
    pid = getattr(platform, "platform_id", None)
    if pid is not None:
        return str(pid)
    if isinstance(platform, str) and platform:
        return platform
    raise TypeError(
        f"tk(platform=...) expected PlatformInstall or str, got {type(platform).__name__}"
    )


def tk(suffix: str, *, platform: Any | None = None) -> str:
    """Return a Redis key template with the correct namespace prefixes.

    ``suffix`` is the post-prefix template, e.g. ``"canvas:job:{job_id}"``.
    The returned string is ready to ``.format(**kwargs)`` on.

    When ``platform`` is omitted (default), the key lives at the
    deployment-level namespace — identical to the legacy behaviour, so
    existing callsites keep their existing keys.

    When ``platform`` is supplied (a ``PlatformInstall`` or its
    ``platform_id`` string), the key is sandboxed under that platform's
    segment. Two Canvas instances managed by the same deployment cannot
    read each other's per-platform data.

    Phase 7 of the multi-tenant migration adds this argument but does
    NOT yet retrofit every callsite; that lands incrementally as each
    storage module is audited. A callsite that doesn't pass ``platform=``
    today continues to read and write at the deployment-level namespace,
    which is correct for shared data (deploy metadata, dead-letter
    queues, etc.) and a known-acceptable compromise for per-tenant data
    until the audit pass.
    """
    pid = _platform_id_of(platform)
    if pid:
        return f"{_PREFIX}:p:{pid}:{suffix}"
    return f"{_PREFIX}:{suffix}"


def current_tenant() -> str:
    """Return the active tenant id, for logging or audit-record fields."""
    raw = (getattr(settings, "canvas_tenant", "") or "").strip().lower()
    return raw or "default"


def tenant_prefix() -> str:
    """Return the raw prefix (no trailing colon)."""
    return _PREFIX
