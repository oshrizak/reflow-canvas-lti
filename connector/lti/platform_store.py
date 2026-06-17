"""Redis-backed storage for ``PlatformInstall`` records.

Key layout::

  eq-pdf:lti:platform:{platform_id}        Hash → JSON payload
  eq-pdf:lti:platforms                     Set  → all known platform_ids
  eq-pdf:lti:platform:by-issuer:{issuer}   Set  → platform_ids at that issuer

The by-issuer index lets future ops tooling enumerate "every platform
installed at canvas.csueastbay.edu" without scanning every record. Right
now (Phase 1) it's just maintained for future use; nothing reads it
yet.

Upsert semantics: ``put_platform`` always preserves ``first_seen_at``
from the existing record if one exists. Everything else (endpoints,
label, last_launch_at) is overwritten with the incoming values - the
expectation is that the LTI launch handler always knows the most
current truth.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

from ..canvas.tenant import tk
from .platform import PlatformInstall, compute_platform_id, now_iso

logger = logging.getLogger(__name__)

# Phase 1 stores platforms at the deployment-level prefix (not yet a
# per-tenant prefix). That arrives in Phase 7 when ``tk()`` learns to
# take a platform. For now the records ARE the source of truth for
# tenancy, so they live at the unscoped namespace.
PLATFORM_KEY = tk("lti:platform:{platform_id}")
PLATFORMS_INDEX_KEY = tk("lti:platforms")
BY_ISSUER_KEY = tk("lti:platform:by-issuer:{issuer_hash}")


def _issuer_hash(issuer: str) -> str:
    """Stable per-issuer key segment.

    Hashing avoids Redis-unsafe characters and bounds the key length.
    Same scheme as ``compute_platform_id`` but with only the issuer.
    """
    import hashlib

    return hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:16]


async def put_platform(redis: Redis, install: PlatformInstall) -> PlatformInstall:
    """Upsert a platform record. Returns the persisted instance.

    If an existing record is present, its ``first_seen_at`` is
    preserved and the returned object reflects that. Caller-supplied
    ``last_launch_at`` is taken as authoritative.
    """

    pid = install.platform_id
    existing = await get_platform(redis, pid)
    if existing is not None:
        # Preserve first-seen and any prior granted_scopes that the
        # incoming record does not declare. The launch JWT does not
        # carry granted-scope information, so a fresh install record
        # constructed from a launch always has an empty list - we must
        # not let that wipe a scope set discovered later by Phase 2.
        install.first_seen_at = existing.first_seen_at or install.first_seen_at
        if not install.granted_scopes and existing.granted_scopes:
            install.granted_scopes = existing.granted_scopes
        # Preserve a soft-revocation marker if one is in place. Lifting
        # a revocation should be an explicit ops action, not something
        # a routine launch can do by accident.
        if existing.revoked_at and not install.revoked_at:
            install.revoked_at = existing.revoked_at

        # Preserve an institutional canvas_api_base / canvas_domain when
        # the existing record disagrees with the SSO-derived default.
        # The disagreement comes from one of:
        #   * An operator override via fix_platform_host.
        #   * A launch JWT that carried a $Canvas.api.domain custom claim.
        # Either way, we do NOT want a subsequent launch (which lacks the
        # custom claim because the dev key's custom_fields list was set
        # before we added canvas_api_domain) to silently roll back to the
        # SSO host. The SSO host is correct for token mints but wrong
        # for the data API and the user-OAuth authorize page.
        def _host_of(url: str) -> str:
            try:
                from urllib.parse import urlparse
                return (urlparse(url).netloc or "").lower()
            except Exception:
                return ""
        existing_host = _host_of(existing.canvas_api_base or "")
        incoming_host = _host_of(install.canvas_api_base or "")
        if existing_host and existing_host != incoming_host:
            install.canvas_api_base = existing.canvas_api_base
            install.canvas_domain = existing.canvas_domain or existing_host

        # Same preservation for auth_token_url -- when an operator has
        # pointed token mints at the institutional host (because the
        # canonical SSO is in maintenance, or because the institution
        # uses a non-standard auth host), don't let a routine launch
        # silently roll back to the SSO host.
        existing_token_host = _host_of(existing.auth_token_url or "")
        incoming_token_host = _host_of(install.auth_token_url or "")
        if existing_token_host and existing_token_host != incoming_token_host:
            install.auth_token_url = existing.auth_token_url
            install.auth_login_url = existing.auth_login_url or install.auth_login_url
            install.jwks_url = existing.jwks_url or install.jwks_url

    await redis.set(
        PLATFORM_KEY.format(platform_id=pid),
        json.dumps(install.to_json()),
    )
    await redis.sadd(PLATFORMS_INDEX_KEY, pid)
    await redis.sadd(
        BY_ISSUER_KEY.format(issuer_hash=_issuer_hash(install.issuer)),
        pid,
    )
    logger.info(
        "platform upsert: platform_id=%s issuer=%s deployment_id=%s",
        pid, install.issuer, install.deployment_id,
    )
    return install


async def get_platform(redis: Redis, platform_id: str) -> PlatformInstall | None:
    raw: Any = await redis.get(PLATFORM_KEY.format(platform_id=platform_id))
    if raw is None:
        return None
    return PlatformInstall.from_json(json.loads(raw))


async def get_platform_by_identity(
    redis: Redis,
    *,
    issuer: str,
    client_id: str,
    deployment_id: str,
) -> PlatformInstall | None:
    """Look up by the full identity triple. Convenience wrapper."""
    pid = compute_platform_id(issuer, client_id, deployment_id)
    return await get_platform(redis, pid)


async def list_platforms(redis: Redis) -> list[PlatformInstall]:
    """Return every known platform. For ops/CLI use; not on hot paths."""
    raw_ids: Any = await redis.smembers(PLATFORMS_INDEX_KEY)
    if not raw_ids:
        return []
    out: list[PlatformInstall] = []
    for raw in raw_ids:
        pid = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        record = await get_platform(redis, pid)
        if record is not None:
            out.append(record)
    return out


async def list_platforms_by_issuer(redis: Redis, issuer: str) -> list[PlatformInstall]:
    """All platform records whose issuer claim equals the given URL."""
    raw_ids: Any = await redis.smembers(
        BY_ISSUER_KEY.format(issuer_hash=_issuer_hash(issuer))
    )
    if not raw_ids:
        return []
    out: list[PlatformInstall] = []
    for raw in raw_ids:
        pid = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        record = await get_platform(redis, pid)
        if record is not None:
            out.append(record)
    return out


async def touch_last_launch(redis: Redis, platform_id: str) -> None:
    """Bump ``last_launch_at`` without rewriting the rest of the record.

    Cheaper than a full upsert when the launch handler already knows
    nothing about the platform endpoints has changed. Phase 1 callers
    use ``put_platform`` so this is provided for future use.
    """
    record = await get_platform(redis, platform_id)
    if record is None:
        return
    record.last_launch_at = now_iso()
    await redis.set(
        PLATFORM_KEY.format(platform_id=platform_id),
        json.dumps(record.to_json()),
    )


async def mark_revoked(redis: Redis, platform_id: str, *, when: str | None = None) -> bool:
    """Mark a platform as soft-revoked. Returns False if not found."""
    record = await get_platform(redis, platform_id)
    if record is None:
        return False
    record.revoked_at = when or now_iso()
    await redis.set(
        PLATFORM_KEY.format(platform_id=platform_id),
        json.dumps(record.to_json()),
    )
    logger.warning("platform revoked: platform_id=%s", platform_id)
    return True


async def clear_revoked(redis: Redis, platform_id: str) -> bool:
    """Lift a revocation. Returns False if not found."""
    record = await get_platform(redis, platform_id)
    if record is None:
        return False
    record.revoked_at = None
    await redis.set(
        PLATFORM_KEY.format(platform_id=platform_id),
        json.dumps(record.to_json()),
    )
    logger.info("platform revocation cleared: platform_id=%s", platform_id)
    return True


# ---------------------------------------------------------------------------
# Course <-> platform mapping. Used by the multi-tenant watcher (Phase 5)
# to know which courses to scan under which platform's credentials. The
# launch handler populates this on every successful LTI launch.
# ---------------------------------------------------------------------------

PLATFORM_COURSES_KEY = tk("lti:platform:{platform_id}:courses")
COURSE_PLATFORM_KEY = tk("lti:course:{course_id}:platform")


async def mark_course_seen(redis: Redis, platform_id: str, course_id: str) -> None:
    """Record that a course on a given platform has launched the tool.

    Two indices kept in sync:

      * ``lti:platform:{pid}:courses`` -- set of course ids on this
        platform. The watcher iterates this per platform.
      * ``lti:course:{course_id}:platform`` -- the platform id for a
        given course. The bridge worker reads this when it needs to
        construct a service-token client for an existing job that lacks
        an explicit ``platform_id`` field.

    Idempotent: same course can be marked any number of times, the
    indices only grow.
    """
    if not platform_id or not course_id:
        return
    await redis.sadd(PLATFORM_COURSES_KEY.format(platform_id=platform_id), course_id)
    await redis.set(COURSE_PLATFORM_KEY.format(course_id=course_id), platform_id)


async def get_courses_for_platform(redis: Redis, platform_id: str) -> list[str]:
    """Return the course ids the watcher should scan for this platform."""
    raw: Any = await redis.smembers(
        PLATFORM_COURSES_KEY.format(platform_id=platform_id)
    )
    if not raw:
        return []
    return sorted(
        (m.decode() if isinstance(m, (bytes, bytearray)) else m) for m in raw
    )


async def get_platform_for_course(redis: Redis, course_id: str) -> str | None:
    """Look up which platform a course belongs to. Returns None if unknown."""
    raw: Any = await redis.get(COURSE_PLATFORM_KEY.format(course_id=course_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


# ---------------------------------------------------------------------------
# Course owner mapping. Used by the multi-tenant watcher to pick whose
# OAuth token authorizes background scans of a given course. The OAuth
# callback handler claims ownership for the first Instructor-role user
# who completes consent on a course.
# ---------------------------------------------------------------------------

COURSE_OWNER_KEY = tk("lti:course:{course_id}:owner")


async def claim_course_owner_if_unset(
    redis: Redis,
    *,
    course_id: str,
    user_id: str,
) -> bool:
    """Atomically claim a course owner if none is currently set.

    Returns True if this call assigned the owner, False if one was
    already assigned by a prior call. Idempotent across calls from the
    same user.

    Why atomic: two faculty members could complete OAuth consent at
    nearly the same time and we want the first one (by Redis arrival)
    to win, not the second one to silently overwrite. Redis ``SET NX``
    gives us that for free.
    """
    if not course_id or not user_id:
        return False
    key = COURSE_OWNER_KEY.format(course_id=course_id)
    # nx=True means "only set if key doesn't exist". TTL omitted -- the
    # owner record is persistent until explicitly cleared (e.g. when
    # the faculty member's OAuth token is revoked).
    set_result = await redis.set(key, user_id, nx=True)
    return bool(set_result)


async def get_course_owner(redis: Redis, course_id: str) -> str | None:
    """Return the user_id of the course's first owner, or None."""
    if not course_id:
        return None
    raw: Any = await redis.get(COURSE_OWNER_KEY.format(course_id=course_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


async def clear_course_owner(redis: Redis, course_id: str) -> None:
    """Delete a stored owner. Called when their OAuth token is revoked."""
    if not course_id:
        return
    await redis.delete(COURSE_OWNER_KEY.format(course_id=course_id))
