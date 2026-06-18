"""Per-course AI-API spend cap.

Finance reasonably asks "what stops one runaway upload from costing $20k?".
The answer is a hard monthly budget per Canvas course. Every time the
watcher submits a new document to Reflow, we add an *estimated* cost to
the course's monthly counter; if the counter is already over the cap,
we skip the submission and log a warning so the operator can act.

Why estimate-at-submit rather than measure-at-completion: the pipeline's
actual AI provider bill isn't known until Reflow returns, and by then the
money is already spent. A pre-flight estimate is the only way to prevent
runaway cost; the estimate doesn't have to be perfect, just close enough
to flag obvious abuse (200-page textbook on Opus, etc.).

The counter is per-course-per-calendar-month and resets implicitly because
the Redis key encodes the month. Operators reviewing a cost incident can
read the historical keys to see which course consumed the budget when.

Configuration
-------------
* ``canvas_monthly_spend_cap_usd_default`` (int dollars) - the cap that
  applies to every course unless overridden. 0 disables the cap entirely.
* ``canvas_monthly_spend_cap_overrides`` (JSON string) - per-course
  overrides, e.g. ``{"50594": 250, "12345": 50}``. Useful for pilot
  programs where one course is allowed a bigger budget.
* ``canvas_estimated_cost_per_doc_cents`` (int) - pre-flight estimate.
  Defaults to 10 (~10 cents per typical document on Haiku-routed
  smart-routing). Bump this if your fleet runs Sonnet/Opus by default.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from redis.asyncio import Redis

from ..config import settings
from .tenant import tk

logger = logging.getLogger(__name__)

# Redis key: cents spent in {course_id} during {YYYY-MM}.
# Cents (not dollars) so we don't lose precision to int rounding.
_SPEND_KEY = tk("canvas:spend:{course_id}:{month}")
_SPEND_TTL_SECONDS = 100 * 86400  # ~3 months, lets reports walk a quarter


def _current_month() -> str:
    """Return the YYYY-MM string for the wall-clock month in UTC."""
    return time.strftime("%Y-%m", time.gmtime())


def _cap_for_course(course_id: str) -> int:
    """Return the configured monthly cap in cents for the given course.

    Override precedence: per-course JSON map overrides the default.
    Returns 0 when no cap applies (which the caller treats as
    "unlimited" - submissions never get blocked).
    """
    overrides_raw = getattr(settings, "canvas_monthly_spend_cap_overrides", "") or ""
    if overrides_raw:
        try:
            overrides = json.loads(overrides_raw)
            if course_id in overrides:
                return int(overrides[course_id]) * 100
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("canvas_monthly_spend_cap_overrides is not valid JSON; ignoring")
    default_usd = int(getattr(settings, "canvas_monthly_spend_cap_usd_default", 0) or 0)
    return default_usd * 100


def _estimate_cents() -> int:
    """Estimated marginal cost per submission, in cents.

    Conservative default of 10 cents matches what smart-routing-to-Haiku
    actually costs on typical 5-30 page documents. Operators with
    different defaults bump this via env.
    """
    return int(getattr(settings, "canvas_estimated_cost_per_doc_cents", 10) or 10)


async def get_monthly_spend_cents(redis: Redis, course_id: str) -> int:
    """Return cents charged to the course this calendar month."""
    key = _SPEND_KEY.format(course_id=course_id, month=_current_month())
    raw: Any = await redis.get(key)
    if raw is None:
        return 0
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return 0


async def reserve_submission(redis: Redis, course_id: str) -> tuple[bool, dict[str, int]]:
    """Atomically check the cap and (if allowed) charge the estimated cost.

    Returns ``(allowed, info)``. ``info`` is always populated with the
    pre-decision spend, the cap, and the estimate, so the caller can log
    a useful message either way. When the cap is 0 (unlimited) we always
    allow and still record the spend - the counter stays useful for
    historical reports even when no cap is set.

    Uses INCRBY (atomic) so concurrent watcher ticks can't both squeeze
    a final job in past the cap. If the post-increment value exceeds the
    cap, we DECRBY the estimate back and refuse the submission. Net cost:
    two Redis round-trips on the hot path, one on the unhappy path.
    """
    cap = _cap_for_course(course_id)
    estimate = _estimate_cents()
    key = _SPEND_KEY.format(course_id=course_id, month=_current_month())

    new_total: Any = await redis.incrby(key, estimate)
    new_total = int(new_total)
    # Keep the key from living forever - we re-set TTL on each write so
    # active courses retain history while abandoned keys eventually expire.
    await redis.expire(key, _SPEND_TTL_SECONDS)

    info = {
        "spend_after_cents": new_total,
        "cap_cents": cap,
        "estimate_cents": estimate,
    }

    if cap > 0 and new_total > cap:
        # Roll back our charge - we won't be submitting the doc.
        await redis.decrby(key, estimate)
        info["spend_after_cents"] = new_total - estimate
        logger.warning(
            "Spend cap reached for course %s: %d > %d cents this month",
            course_id, new_total, cap,
        )
        return False, info

    return True, info


async def reconcile_actual_cost(
    redis: Redis,
    course_id: str,
    actual_cents: int,
) -> None:
    """Update the counter once Reflow reports the actual job cost.

    The watcher records an estimate at submission time (so we can gate);
    when the bridge worker sees a completed job with the real cost
    attached, it calls this to true up. Difference (actual - estimate)
    is added to the counter; can be negative for cheaper-than-expected
    jobs. This keeps long-term spend tracking honest.
    """
    estimate = _estimate_cents()
    delta = int(actual_cents) - estimate
    if delta == 0:
        return
    key = _SPEND_KEY.format(course_id=course_id, month=_current_month())
    await redis.incrby(key, delta)
    await redis.expire(key, _SPEND_TTL_SECONDS)
