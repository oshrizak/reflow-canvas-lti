"""Fixed-window rate limiter backed by Redis.

Protects the connector's state-changing POST endpoints (approve,
reject, edit, pii-decision, unpublish, request-edits) from runaway
scripts or browser extensions hammering the handlers — the kind of
failure we surfaced in the production-readiness audit.

The limiter is intentionally simple: each call increments a counter
key scoped to ``(bucket, actor, window_id)`` where ``window_id`` is
``floor(now / window_seconds)``. The key auto-expires once the
window closes. No Lua scripts, no leaky-bucket math, two Redis
commands per request — and with ``incr`` being atomic, no race.

Trade-off: a burst exactly at the window boundary can briefly
exceed the configured limit by up to 2x. That's acceptable for our
threat model — limits are set well below the level at which actual
faculty workflow would brush against them, so the goal is "abuse
detection," not "exact quota."

Per-endpoint limits are picked at call sites; see ``connector/api``
for the actual numbers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


async def enforce_rate_limit(
    redis: Any,
    *,
    bucket: str,
    actor: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Allow up to ``limit`` requests per ``actor`` within ``window_seconds``.

    Args:
        redis: An async Redis client (``redis.asyncio.Redis``).
        bucket: Short identifier for the endpoint family (e.g. ``"approve"``).
            Different buckets get independent counters so a flurry of
            edits doesn't burn through the approve quota.
        actor: Whatever identifies the caller. For session-bound
            endpoints this is the LTI session's user id; for unauth
            paths fall back to the client IP. Empty/None is treated as
            ``"_anon"`` so we still rate-limit unauthenticated bursts.
        limit: Maximum requests allowed in the window.
        window_seconds: Length of the fixed window.

    Raises:
        HTTPException(429): When the limit is exceeded for this
            ``(bucket, actor)``. Includes a ``Retry-After`` header set
            to the seconds remaining until the next window opens.
    """
    safe_actor = actor or "_anon"
    window_id = int(time.time() // window_seconds)
    key = f"eq-pdf:rl:{bucket}:{safe_actor}:{window_id}"
    count = await redis.incr(key)
    if count == 1:
        # First request in this window — set TTL slightly longer than the
        # window so the key garbage-collects naturally after the window
        # closes. Without the +5s slack a perfectly-timed second request
        # in the next bucket could race with the expire.
        await redis.expire(key, window_seconds + 5)
    if count > limit:
        next_boundary = (window_id + 1) * window_seconds
        retry_after = max(1, next_boundary - int(time.time()))
        logger.warning(
            "rate limit exceeded: bucket=%s actor=%s count=%d limit=%d window=%ds",
            bucket,
            safe_actor,
            count,
            limit,
            window_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many '{bucket}' requests. The limit is {limit} per "
                f"{window_seconds} seconds. Try again in {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )
