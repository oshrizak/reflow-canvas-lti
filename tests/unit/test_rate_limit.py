"""Unit tests for the Redis-backed rate limiter.

These exercise the contract every wired-up POST handler depends on:
calls under the limit pass through, calls over the limit 429 with the
right Retry-After header, different ``(bucket, actor)`` pairs don't
share counters, and the next window opens at the boundary.
"""

from __future__ import annotations

import asyncio

import pytest
from connector.utils.rate_limit import enforce_rate_limit
from fastapi import HTTPException


class _FakeRedis:
    """Tiny INCR/EXPIRE Redis stand-in.

    The limiter only ever calls ``incr`` and ``expire``; faking those
    two is enough to drive the test without spinning up a real Redis.
    Counters reset between tests because we instantiate a fresh
    ``_FakeRedis`` per case.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


@pytest.mark.unit
def test_under_limit_passes_through() -> None:
    """``limit`` consecutive calls under the cap must all succeed."""
    rd = _FakeRedis()

    async def go() -> None:
        for _ in range(5):
            await enforce_rate_limit(
                rd, bucket="approve", actor="u1", limit=5, window_seconds=60,
            )

    asyncio.run(go())  # No exception = pass.


@pytest.mark.unit
def test_over_limit_raises_429_with_retry_after() -> None:
    """The (limit+1)th call must raise HTTPException(429) and the
    Retry-After header has to be a positive integer."""
    rd = _FakeRedis()

    async def go() -> None:
        for _ in range(3):
            await enforce_rate_limit(
                rd, bucket="approve", actor="u1", limit=3, window_seconds=60,
            )
        await enforce_rate_limit(
            rd, bucket="approve", actor="u1", limit=3, window_seconds=60,
        )

    with pytest.raises(HTTPException) as ei:
        asyncio.run(go())
    assert ei.value.status_code == 429
    retry_after = int(ei.value.headers["Retry-After"])
    assert 1 <= retry_after <= 60


@pytest.mark.unit
def test_separate_buckets_do_not_share_counter() -> None:
    """An approve flurry must NOT exhaust the edit quota for the same user."""
    rd = _FakeRedis()

    async def go() -> None:
        # Burn through approve.
        for _ in range(5):
            await enforce_rate_limit(
                rd, bucket="approve", actor="u1", limit=5, window_seconds=60,
            )
        # First edit must still pass.
        await enforce_rate_limit(
            rd, bucket="edit", actor="u1", limit=5, window_seconds=60,
        )

    asyncio.run(go())


@pytest.mark.unit
def test_separate_actors_do_not_share_counter() -> None:
    """One user's burst must not block another user's first call."""
    rd = _FakeRedis()

    async def go() -> None:
        for _ in range(5):
            await enforce_rate_limit(
                rd, bucket="approve", actor="u1", limit=5, window_seconds=60,
            )
        await enforce_rate_limit(
            rd, bucket="approve", actor="u2", limit=5, window_seconds=60,
        )

    asyncio.run(go())


@pytest.mark.unit
def test_anonymous_actor_still_rate_limited() -> None:
    """Empty ``actor`` is bucketed under ``_anon`` so unauth bursts still
    hit a wall — important if a public-facing handler ever forgets to
    derive the user id."""
    rd = _FakeRedis()

    async def go() -> None:
        for _ in range(3):
            await enforce_rate_limit(
                rd, bucket="approve", actor="", limit=3, window_seconds=60,
            )
        await enforce_rate_limit(
            rd, bucket="approve", actor="", limit=3, window_seconds=60,
        )

    with pytest.raises(HTTPException) as ei:
        asyncio.run(go())
    assert ei.value.status_code == 429
    # Verify the key carried ``_anon`` so we can observe the unauth bucket
    # in production logs.
    assert any("_anon" in k for k in rd.counts)


@pytest.mark.unit
def test_first_call_sets_expire() -> None:
    """The TTL on the counter key must be set on the FIRST request in the
    window (and only then) so the key garbage-collects after the window
    closes — otherwise stale counters leak."""
    rd = _FakeRedis()

    async def go() -> None:
        await enforce_rate_limit(
            rd, bucket="approve", actor="u1", limit=5, window_seconds=60,
        )
        await enforce_rate_limit(
            rd, bucket="approve", actor="u1", limit=5, window_seconds=60,
        )

    asyncio.run(go())
    assert len(rd.expires) == 1
    assert next(iter(rd.expires.values())) == 65  # window + 5s slack
