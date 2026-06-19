"""Unit tests for the per-course pending-review set.

The Accessible Documents LTI tool's queue is driven by what's in
``PENDING_KEY`` for the course. Both faculty-decision states qualify
(``awaiting_review`` = accessibility approval, ``awaiting_approval`` =
upstream PII gate) and any other status must NOT appear there.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from connector.canvas.state import (
    PENDING_KEY,
    CanvasJob,
    list_pending,
    put_job,
)


class _FakeRedis:
    """Tiny in-memory stand-in for the bits of ``redis.asyncio`` we touch.

    The real client is heavyweight (requires a server). We only need
    ``set``/``get``/``delete`` and the set ops + a no-op ``scan`` to
    drive the code under test.
    """

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value

    async def get(self, key: str) -> Any:
        return self.kv.get(key)

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self.kv.pop(k, None)
            self.sets.pop(k, None)

    async def sadd(self, key: str, *members: str) -> None:
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key: str, *members: str) -> None:
        s = self.sets.get(key)
        if s is None:
            return
        s.difference_update(members)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, ()))


def _job(job_id: str, course_id: str, status: str) -> CanvasJob:
    return CanvasJob(
        reflow_job_id=job_id,
        canvas_file_id="f1",
        canvas_file_name="doc.pdf",
        canvas_course_id=course_id,
        canvas_user_id="u1",
        status=status,  # type: ignore[arg-type]  # Literal narrowed at runtime
        created_at=0.0,
    )


@pytest.mark.unit
def test_awaiting_review_is_pending() -> None:
    """The accessibility-approval gate has always been in the pending set."""
    rd = _FakeRedis()
    job = _job("j1", "c1", "awaiting_review")
    asyncio.run(put_job(rd, job))  # type: ignore[arg-type]

    assert "j1" in rd.sets.get(PENDING_KEY.format(course_id="c1"), set())


@pytest.mark.unit
def test_awaiting_approval_is_pending() -> None:
    """The PII gate (``awaiting_approval``) now also surfaces in the queue —
    without this, faculty using only the LTI tool can't act on PII-paused
    documents."""
    rd = _FakeRedis()
    job = _job("j2", "c1", "awaiting_approval")
    asyncio.run(put_job(rd, job))  # type: ignore[arg-type]

    assert "j2" in rd.sets.get(PENDING_KEY.format(course_id="c1"), set())


@pytest.mark.unit
@pytest.mark.parametrize(
    "terminal_status",
    ["published", "rejected", "failed", "page_failed", "processing"],
)
def test_non_actionable_statuses_are_not_pending(terminal_status: str) -> None:
    """Anything that doesn't need faculty action stays OUT of the queue."""
    rd = _FakeRedis()
    job = _job("j3", "c1", terminal_status)
    asyncio.run(put_job(rd, job))  # type: ignore[arg-type]

    assert "j3" not in rd.sets.get(PENDING_KEY.format(course_id="c1"), set())


@pytest.mark.unit
def test_status_transition_removes_from_pending() -> None:
    """An ``awaiting_approval`` job that gets approved (→ ``processing``)
    must drop out of the queue on the next put_job — otherwise the LTI
    tool keeps offering a review for work that already moved on."""
    rd = _FakeRedis()
    job = _job("j4", "c1", "awaiting_approval")
    asyncio.run(put_job(rd, job))  # type: ignore[arg-type]
    assert "j4" in rd.sets.get(PENDING_KEY.format(course_id="c1"), set())

    # Status flips after Reflow accepts the decision.
    job.status = "processing"  # type: ignore[assignment]
    asyncio.run(put_job(rd, job))  # type: ignore[arg-type]
    assert "j4" not in rd.sets.get(PENDING_KEY.format(course_id="c1"), set())


@pytest.mark.unit
def test_list_pending_returns_both_gate_kinds_for_same_course() -> None:
    """``list_pending`` reads the same set, so it must return both gate
    kinds the index.html template knows how to badge."""
    rd = _FakeRedis()
    rev = _job("rev", "c1", "awaiting_review")
    pii = _job("pii", "c1", "awaiting_approval")
    asyncio.run(put_job(rd, rev))  # type: ignore[arg-type]
    asyncio.run(put_job(rd, pii))  # type: ignore[arg-type]

    pending = asyncio.run(list_pending(rd, "c1"))  # type: ignore[arg-type]
    ids = {j.reflow_job_id for j in pending}
    statuses = {j.status for j in pending}

    assert ids == {"rev", "pii"}
    assert statuses == {"awaiting_review", "awaiting_approval"}
