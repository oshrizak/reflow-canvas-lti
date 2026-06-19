"""Erase one user's personal data from the connector's Redis state.

Implements the operational side of a GDPR Article 17 ("right to
erasure") or FERPA-amendment request against the connector's
locally-held data. Reflow Core stores converted markdown and figure
rasters of its own — those need a separate request against Core.

What gets deleted:

  * Every ``eq-pdf:lti:user-token:{platform}:<user_id>`` — the
    encrypted OAuth tokens that proved the user's identity to Canvas
    on the connector's behalf.
  * The user's consent record (``eq-pdf:canvas:consent:<user_id>``).
  * Every LTI session whose ``user_id`` field matches.
  * Every CanvasJob whose ``canvas_user_id`` matches — the
    connector's local record of converted documents the user
    uploaded. Also drops the per-job score cache and edited HTML.
  * The user's entries in the per-course processed-files set are
    NOT removed: the markers don't carry user identity and removing
    them would cause re-discovery loops.

What gets pseudonymised (NOT deleted):

  * Approval audit log entries whose ``actor_user_id`` matches.
    ISO 27001 + 27018 want the record of accountability preserved
    when erasure happens — replace the user id with the SHA-256
    hash of ``<user_id> + <retention_pepper>`` so the chain remains
    auditable but the natural identifier is gone.

Usage::

    docker compose exec connector python -m connector.tools.erase_user \\
        --user-id 'bb49a44f-db25-4c91-9b1d-83cacb92a177'

Dry-run mode (default) reports what WOULD be deleted/pseudonymised.
``--commit`` actually performs the writes. Both modes emit an
``operator_action`` audit row.

Idempotent: re-running after a successful commit is a no-op (counts
are zero).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from typing import Any

from ..canvas.audit import emit_operator_action
from ..canvas.state import APPROVAL_AUDIT_KEY, CONSENT_AUDIT_KEY, CONSENT_KEY
from ..canvas.tenant import tk
from ..dependencies import get_redis_client


def _pseudonymize(user_id: str) -> str:
    """Replace the natural user identifier with a stable hash.

    The pepper is whatever ``TOKEN_ENCRYPTION_KEY`` happens to be —
    deliberately the same secret that protects OAuth tokens at rest.
    Rotating it invalidates the link between past audit rows and the
    user; that's a feature, not a bug, when handling erasure requests
    that themselves come from a regulator.
    """
    pepper = os.environ.get("TOKEN_ENCRYPTION_KEY", "") or os.environ.get(
        "CSRF_SECRET_KEY", ""
    )
    blob = (user_id + "|" + pepper).encode("utf-8")
    return "erased:" + hashlib.sha256(blob).hexdigest()[:16]


async def _scan_keys(redis: Any, pattern: str) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=200)
        for k in batch:
            keys.append(k.decode() if isinstance(k, (bytes, bytearray)) else str(k))
        if cursor == 0:
            return keys


async def _erase(redis: Any, user_id: str, *, commit: bool) -> dict[str, Any]:
    """Walk Redis, count what would change, and apply if ``commit``."""
    counts: dict[str, Any] = {
        "user_tokens_deleted": 0,
        "consent_records_deleted": 0,
        "sessions_deleted": 0,
        "jobs_deleted": 0,
        "edited_html_deleted": 0,
        "score_cache_deleted": 0,
        "approval_audit_pseudonymised": 0,
        "consent_audit_pseudonymised": 0,
    }
    pseudonym = _pseudonymize(user_id)

    # OAuth tokens — one key per (platform, user). Pattern includes
    # the user id at the tail.
    for k in await _scan_keys(redis, tk("lti:user-token:*:{user}").format(user=user_id)):
        counts["user_tokens_deleted"] += 1
        if commit:
            await redis.delete(k)

    # Consent record.
    consent_key = CONSENT_KEY.format(user_id=user_id)
    if await redis.exists(consent_key):
        counts["consent_records_deleted"] = 1
        if commit:
            await redis.delete(consent_key)

    # Sessions — key shape is ``eq-pdf:canvas:session:{session_id}``.
    # We have to read each one and inspect the ``user_id`` field.
    for k in await _scan_keys(redis, tk("canvas:session:*")):
        raw = await redis.get(k)
        if raw is None:
            continue
        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            data = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            continue
        if data.get("user_id") == user_id:
            counts["sessions_deleted"] += 1
            if commit:
                await redis.delete(k)

    # CanvasJob records — same per-key inspection.
    for k in await _scan_keys(redis, tk("canvas:job:*")):
        raw = await redis.get(k)
        if raw is None:
            continue
        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            data = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            continue
        if data.get("canvas_user_id") != user_id:
            continue
        counts["jobs_deleted"] += 1
        if commit:
            await redis.delete(k)
            # Also drop the score cache + any faculty-edited HTML.
            jid = data.get("reflow_job_id")
            if jid:
                cache_key = tk("canvas:score:{job_id}").format(job_id=jid)
                edited_key = tk("canvas:edited:{job_id}").format(job_id=jid)
                if await redis.exists(cache_key):
                    counts["score_cache_deleted"] += 1
                    await redis.delete(cache_key)
                if await redis.exists(edited_key):
                    counts["edited_html_deleted"] += 1
                    await redis.delete(edited_key)
            # Drop the job from any pending-review set so the
            # Accessible Documents queue doesn't show a 404 row.
            course_id = data.get("canvas_course_id")
            jid_val = data.get("reflow_job_id")
            if course_id and jid_val:
                pending_key = tk("canvas:course:{course_id}:pending").format(
                    course_id=course_id,
                )
                await redis.srem(pending_key, jid_val)

    # Approval audit log — pseudonymise rather than delete to preserve
    # accountability. Same for the consent audit log.
    for list_key, count_key in (
        (APPROVAL_AUDIT_KEY, "approval_audit_pseudonymised"),
        (CONSENT_AUDIT_KEY, "consent_audit_pseudonymised"),
    ):
        raw_list = await redis.lrange(list_key, 0, -1)
        if not raw_list:
            continue
        keepers: list[str] = []
        changed = False
        for item in raw_list:
            try:
                text = item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
                data = json.loads(text)
            except (UnicodeDecodeError, ValueError):
                keepers.append(item if isinstance(item, str) else item.decode("utf-8", "replace"))
                continue
            replaced = False
            for field in ("actor_user_id", "user_id"):
                if data.get(field) == user_id:
                    data[field] = pseudonym
                    replaced = True
            if replaced:
                changed = True
                counts[count_key] += 1
            keepers.append(json.dumps(data))
        if commit and changed:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.delete(list_key)
                if keepers:
                    pipe.rpush(list_key, *keepers)
                await pipe.execute()

    return counts


async def _main(args: argparse.Namespace) -> int:
    redis = await anext(get_redis_client())
    print(
        f"{'DRY RUN' if not args.commit else 'COMMIT'}: scanning Redis for user {args.user_id!r}…"
    )
    emit_operator_action(
        "erase_user.start",
        target_user=args.user_id,
        commit=bool(args.commit),
    )
    counts = await _erase(redis, args.user_id, commit=args.commit)
    for k, v in counts.items():
        print(f"  {k}: {v}")
    emit_operator_action(
        "erase_user.end",
        target_user=args.user_id,
        commit=bool(args.commit),
        **counts,
    )
    if not args.commit:
        print(
            "\nDry run: nothing changed. Re-run with --commit to apply. "
            "Erasure cannot be undone without restoring from a backup."
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--user-id",
        required=True,
        help="Canvas user_id (LTI ``sub``) of the user to erase.",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Actually delete + pseudonymise. Without this, runs as a dry preview.",
    )
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
