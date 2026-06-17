"""Redis-backed mapping between Canvas files and Reflow jobs.

Keys:
  eq-pdf:canvas:job:{reflow_job_id}            Hash    job ↔ canvas mapping
  eq-pdf:canvas:course:{course_id}:pending     Set     awaiting-review job ids
  eq-pdf:canvas:course:{course_id}:processed   Set     canvas file ids already
                                                       submitted (idempotency)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from .tenant import tk

logger = logging.getLogger(__name__)

JOB_KEY = tk("canvas:job:{job_id}")
PENDING_KEY = tk("canvas:course:{course_id}:pending")
PROCESSED_KEY = tk("canvas:course:{course_id}:processed")
# Stable map of a Canvas file -> the wiki page slug the bridge created for it.
# Survives across re-conversions (each re-conversion is a new reflow_job_id),
# so the bridge can UPDATE the same page instead of creating a duplicate.
FILE_PAGE_KEY = tk("canvas:course:{course_id}:file-page:{file_id}")

JobStatus = Literal[
    "processing",
    # Reflow paused the conversion for a PII/privacy decision. Distinct from
    # "processing" so the overlay can show an Approve/Deny prompt and the
    # timeout watchdog doesn't mislabel it "stuck in processing".
    "awaiting_approval",
    "awaiting_review",
    "published",
    "rejected",
    "failed",
    # Conversion succeeded but the Canvas Page write was rejected (almost
    # always the course-owner OAuth token missing the Pages write scope).
    # Distinct from "failed" (which means the conversion itself failed) so the
    # overlay can tell faculty the real reason, and from "awaiting_review"
    # (which falsely implied a page existed). The bridge keeps polling these
    # and rebuilds the page automatically once the scope is granted.
    "page_failed",
]


@dataclass
class CanvasJob:
    reflow_job_id: str
    canvas_file_id: str
    canvas_file_name: str
    canvas_course_id: str
    canvas_user_id: str
    status: JobStatus
    created_at: float
    canvas_page_id: str | None = None
    canvas_page_url: str | None = None
    error: str | None = None
    # LTI platform that owns this job. Set by the multi-tenant watcher
    # (Phase 5+); legacy single-tenant submissions leave it None and the
    # bridge worker falls back to the env-token CanvasClient for them.
    platform_id: str | None = None
    # Per-document conversion-quality signals derived from the pipeline
    # output (heading structure, image+alt counts, table semantics, etc).
    # ``None`` for jobs that haven't reached completion yet. Populated
    # by the bridge worker when Reflow returns the converted markdown.
    # NOTE: these are conversion-quality heuristics, not WCAG conformance
    # proofs. The publication gate (Phase 7) runs separate WCAG checks.
    signals: dict | None = None
    # Map of markdown figure reference (e.g. "figures/figure-1.png") to the
    # Canvas course-file URL the bridge uploaded it to. Populated by the
    # bridge worker after it uploads figures into the course. Used to embed
    # Canvas-hosted images in both the Canvas Page and the overlay's
    # accessible-HTML views, so pages stay self-contained (no dependency on
    # the tunnel or the Reflow backend staying up).
    figure_canvas_urls: dict | None = None


async def already_processed(
    redis: Redis, course_id: str, canvas_file_id: str
) -> bool:
    return bool(
        await redis.sismember(
            PROCESSED_KEY.format(course_id=course_id), canvas_file_id
        )
    )


async def mark_processed(redis: Redis, course_id: str, canvas_file_id: str) -> None:
    await redis.sadd(PROCESSED_KEY.format(course_id=course_id), canvas_file_id)


async def clear_processed(redis: Redis, course_id: str, canvas_file_id: str) -> None:
    """Drop a file's 'already processed' marker so the watcher re-converts it
    on its next tick. Used by the overlay's manual 'Create accessible page'
    action. Idempotent — a no-op if the marker isn't set."""
    await redis.srem(PROCESSED_KEY.format(course_id=course_id), canvas_file_id)


async def put_job(redis: Redis, job: CanvasJob) -> None:
    await redis.set(
        JOB_KEY.format(job_id=job.reflow_job_id),
        json.dumps(asdict(job)),
    )
    # Drop the panorama score cache (same key the overlay's /score endpoint
    # uses) so a status change shows up promptly. Its TTL is 24h, so without
    # this the overlay can keep showing a stale state — e.g. a PII gate for an
    # already-completed job — long after the bridge moved the job on. put_job
    # is only called on actual changes, so this clears exactly when we want a
    # recompute. Best-effort: never let a cache delete break a status write.
    try:
        await redis.delete(tk("canvas:score:{job_id}").format(job_id=job.reflow_job_id))
    except Exception:  # noqa: BLE001
        pass
    if job.status == "awaiting_review":
        await redis.sadd(
            PENDING_KEY.format(course_id=job.canvas_course_id), job.reflow_job_id
        )
    else:
        await redis.srem(
            PENDING_KEY.format(course_id=job.canvas_course_id), job.reflow_job_id
        )


async def get_job(redis: Redis, reflow_job_id: str) -> CanvasJob | None:
    raw: Any = await redis.get(JOB_KEY.format(job_id=reflow_job_id))
    if raw is None:
        return None
    return CanvasJob(**json.loads(raw))


async def get_file_page(redis: Redis, course_id: str, file_id: str) -> str | None:
    """Return the wiki page slug previously created for this Canvas file."""
    raw: Any = await redis.get(
        FILE_PAGE_KEY.format(course_id=course_id, file_id=file_id)
    )
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


async def put_file_page(
    redis: Redis, course_id: str, file_id: str, page_ref: str
) -> None:
    """Remember the wiki page slug for this Canvas file so re-conversions
    update the same page instead of creating duplicates."""
    if not page_ref:
        return
    await redis.set(
        FILE_PAGE_KEY.format(course_id=course_id, file_id=file_id), page_ref
    )


async def list_pending(redis: Redis, course_id: str) -> list[CanvasJob]:
    job_ids: set[Any] = await redis.smembers(PENDING_KEY.format(course_id=course_id))
    jobs: list[CanvasJob] = []
    for jid in job_ids:
        decoded = jid.decode() if isinstance(jid, bytes) else str(jid)
        job = await get_job(redis, decoded)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.created_at)
    return jobs


EDITED_HTML_KEY = tk("canvas:edited:{job_id}")


async def get_edited_html(redis: Redis, job_id: str) -> str | None:
    """Return the faculty-edited HTML for a job if one exists."""
    raw: Any = await redis.get(EDITED_HTML_KEY.format(job_id=job_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


async def put_edited_html(redis: Redis, job_id: str, html: str) -> None:
    """Persist a faculty-edited HTML body. Source-of-truth for downstream formats."""
    await redis.set(EDITED_HTML_KEY.format(job_id=job_id), html)


async def clear_edited_html(redis: Redis, job_id: str) -> None:
    """Revert to the auto-generated HTML by deleting any edit."""
    await redis.delete(EDITED_HTML_KEY.format(job_id=job_id))


# ---------------------------------------------------------------------------
# Faculty consent / disclaimer acknowledgment
# ---------------------------------------------------------------------------
# Bump this when the disclaimer language changes — users must re-acknowledge
# any version they have not already agreed to.
CURRENT_CONSENT_VERSION = "1.0"

CONSENT_KEY = tk("canvas:consent:{user_id}")
CONSENT_AUDIT_KEY = tk("canvas:consent:audit")  # list, append-only


@dataclass
class ConsentRecord:
    user_id: str
    user_name: str | None
    user_email: str | None
    course_id: str | None
    version: str
    agreed_at: float  # unix seconds
    user_agent: str | None = None
    ip: str | None = None


async def get_consent(redis: Redis, user_id: str) -> ConsentRecord | None:
    """Return the latest consent record for a user, or None if never given."""
    raw: Any = await redis.get(CONSENT_KEY.format(user_id=user_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return ConsentRecord(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Corrupt consent record for %s: %s", user_id, exc)
        return None


async def put_consent(redis: Redis, record: ConsentRecord) -> None:
    """Persist consent + append to the immutable audit log."""
    payload = json.dumps(asdict(record))
    await redis.set(CONSENT_KEY.format(user_id=record.user_id), payload)
    # The audit log is append-only — never trimmed, never updated.
    # If an admin needs to revoke, write a new record; the audit log keeps the
    # full history (who/when/what-version/from-what-IP).
    await redis.rpush(CONSENT_AUDIT_KEY, payload)


# ---------------------------------------------------------------------------
# Approval audit log — every approve/reject/request-edit is recorded
# ---------------------------------------------------------------------------
APPROVAL_AUDIT_KEY = tk("canvas:approval:audit")


async def append_approval_event(
    redis: Redis,
    *,
    job_id: str,
    action: str,                  # "approve" | "reject" | "request_edits"
    actor_user_id: str,
    actor_name: str | None,
    comment: str | None = None,
    actor_ip: str | None = None,
    course_id: str | None = None,
) -> None:
    """Append an immutable approval event to the audit log.

    ``actor_ip`` and ``course_id`` are optional because some test paths
    and historical callers don't carry them, but ISO compliance asks for
    both on every event so production callers should always pass them.
    """
    import time
    payload = json.dumps({
        "job_id": job_id,
        "action": action,
        "actor_user_id": actor_user_id,
        "actor_name": actor_name,
        "actor_ip": actor_ip,
        "course_id": course_id,
        "comment": comment,
        "at": time.time(),
    })
    await redis.rpush(APPROVAL_AUDIT_KEY, payload)


async def list_approval_events(
    redis: Redis,
    *,
    since: float | None = None,
    until: float | None = None,
    course_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return every approval event matching the filters, oldest first.

    Designed for the audit-log CSV export. Filters are inclusive:
    ``since <= at <= until``. ``course_id`` matches the event's stored
    course (jobs created before the field was added will have None and
    are skipped when this filter is set).
    """
    raw = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            data = json.loads(decoded)
        except (json.JSONDecodeError, AttributeError):
            continue
        at = data.get("at")
        if since is not None and (at is None or at < since):
            continue
        if until is not None and (at is None or at > until):
            continue
        if course_id is not None and str(data.get("course_id") or "") != str(course_id):
            continue
        out.append(data)
    return out


async def get_approval_history(redis: Redis, job_id: str) -> list[dict[str, Any]]:
    """Return all approval events for a specific job, oldest first."""
    raw = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            data = json.loads(decoded)
            if data.get("job_id") == job_id:
                out.append(data)
        except (json.JSONDecodeError, AttributeError):
            continue
    return out


async def revoke_consent(redis: Redis, user_id: str) -> None:
    """Admin operation: delete the active consent record. The audit log entry
    showing the original consent remains; the user will be re-prompted next
    launch."""
    await redis.delete(CONSENT_KEY.format(user_id=user_id))
    revoke_marker = json.dumps({
        "event": "revoke",
        "user_id": user_id,
        "at": __import__("time").time(),
    })
    await redis.rpush(CONSENT_AUDIT_KEY, revoke_marker)


def needs_consent(record: ConsentRecord | None) -> bool:
    """True if the user must be shown the disclaimer before proceeding."""
    if record is None:
        return True
    return record.version != CURRENT_CONSENT_VERSION



# ---------------------------------------------------------------------------
# Data-retention sweeper — bounded, predictable purge of old records
# ---------------------------------------------------------------------------
# ISO compliance asks us to retain audit events for a defined window and to
# delete operational data we don't need anymore. The bridge worker calls
# ``purge_old_canvas_records`` on a slow timer; both windows are independent
# so an operator can keep audit history while still rolling jobs off.
#
# Why purge by AGE rather than COUNT: storage cost in Redis is proportional
# to lifetime, and a steady cap on count would conflict with the audit
# log's append-only contract (we never want to evict an event because a
# newer one arrived).
async def purge_old_canvas_records(
    redis: Redis,
    *,
    job_retention_seconds: int,
    audit_retention_seconds: int,
) -> dict[str, int]:
    """Delete Canvas state older than the configured retention windows.

    ``job_retention_seconds <= 0`` disables job purging; the same goes for
    ``audit_retention_seconds`` and the audit log. Returns a dict of
    {bucket: count_removed} so the caller can log how much got swept.

    Jobs deleted here are the *records* in Redis - the underlying S3
    artifacts (markdown, figures) are owned by Reflow and have their own
    lifecycle. The faculty member can always re-upload to regenerate.
    """
    import time

    now = time.time()
    out = {"jobs_removed": 0, "audit_events_removed": 0}

    # ---- Jobs --------------------------------------------------------------
    if job_retention_seconds > 0:
        cutoff = now - job_retention_seconds
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=tk("canvas:job:*"), count=200
            )
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                job_id = key.rsplit(":", 1)[-1]
                job = await get_job(redis, job_id)
                if job is None:
                    continue
                if (job.created_at or 0) > cutoff:
                    continue
                # Also remove from per-course pending set (no-op if absent).
                if job.canvas_course_id:
                    await redis.srem(
                        PENDING_KEY.format(course_id=job.canvas_course_id), job_id
                    )
                await redis.delete(key)
                # Edited HTML lives at its own key; drop it too.
                await clear_edited_html(redis, job_id)
                out["jobs_removed"] += 1
            if cursor == 0:
                break

    # ---- Audit events ------------------------------------------------------
    # The audit log is a Redis list, so we can't SCAN it. Read once, filter
    # out the keepers, and rewrite the list atomically via a transaction so
    # we don't lose events that arrive mid-purge.
    if audit_retention_seconds > 0:
        cutoff = now - audit_retention_seconds
        raw_events = await redis.lrange(APPROVAL_AUDIT_KEY, 0, -1)
        keepers: list[str] = []
        removed = 0
        for item in raw_events:
            decoded = item.decode() if isinstance(item, bytes) else str(item)
            try:
                at = json.loads(decoded).get("at")
            except (json.JSONDecodeError, TypeError):
                # Don't drop unparseable rows - preserves the audit guarantee
                # even when an old version emitted weird JSON.
                keepers.append(decoded)
                continue
            if isinstance(at, (int, float)) and at < cutoff:
                removed += 1
                continue
            keepers.append(decoded)

        if removed > 0:
            # Replace the list in one round-trip. Pipeline guarantees both
            # commands run on the same connection without interleaving.
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(APPROVAL_AUDIT_KEY)
                if keepers:
                    pipe.rpush(APPROVAL_AUDIT_KEY, *keepers)
                await pipe.execute()
            out["audit_events_removed"] = removed

    return out
