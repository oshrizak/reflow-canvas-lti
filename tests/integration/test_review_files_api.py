"""The review screen's whole-course listing.

``/api/pending`` only ever saw the pending set, so a document that was
converting, published, or broken was invisible to faculty — the screen
could not answer "where is everything?", which is the question it gets
opened to settle. ``/api/files`` walks every bridge record for the course
instead.

Two behaviours carry the weight here:

  1. **One row per Canvas file.** Re-submitting a document leaves several
     job records behind. If a stale ``failed`` row won, a working document
     would show as broken.
  2. **Buckets, not raw statuses.** Reflow's internal vocabulary is finer
     than a coordinator needs; the row has to say whether the document
     needs them, is working, is done, or has gone wrong.
"""

from __future__ import annotations

import pytest
from connector.canvas.state import CanvasJob, put_job
from connector.lti.routes import SESSION_COOKIE
from connector.lti.session import SessionPayload, new_session_id, put_session

COURSE = "c1"
URL = f"/canvas/review/api/files?course_id={COURSE}"


async def _session(redis) -> str:  # noqa: ANN001
    sid = new_session_id()
    await put_session(
        redis,
        sid,
        SessionPayload(
            user_id="u1",
            user_name="Zach",
            user_email="pat@example.edu",
            course_id=COURSE,
            roles=["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"],
        ),
    )
    return sid


def _job(job_id: str, file_id: str, status: str, *, created: float, course=COURSE):
    return CanvasJob(
        reflow_job_id=job_id,
        canvas_file_id=file_id,
        canvas_file_name=f"{file_id}.pdf",
        canvas_course_id=course,
        canvas_user_id="u1",
        status=status,
        created_at=created,
    )


@pytest.mark.asyncio
async def test_requires_a_session(client):  # noqa: ANN001
    client.cookies.clear()
    assert client.get(URL).status_code == 401


@pytest.mark.asyncio
async def test_empty_course_returns_no_files(client, redis_client):  # noqa: ANN001
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))
    body = client.get(URL).json()

    assert body["files"] == []
    assert body["counts"] == {}


@pytest.mark.asyncio
async def test_lists_documents_that_are_not_pending(client, redis_client):  # noqa: ANN001
    """The regression this endpoint exists for.

    A published document never sits in the pending set, so the old screen
    showed nothing at all once work finished.
    """
    await put_job(redis_client, _job("j1", "111", "published", created=100.0))
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))

    files = client.get(URL).json()["files"]

    assert len(files) == 1
    assert files[0]["bucket"] == "done"
    assert files[0]["action_url"] is None, "a finished document needs no decision"


@pytest.mark.asyncio
async def test_one_row_per_file_and_live_job_beats_stale_failure(
    client, redis_client
):  # noqa: ANN001
    """A re-converted document must not read as broken.

    Both records belong to Canvas file 111. The failed one is *newer*, so
    recency alone would pick it — the ranking has to prefer the outcome
    that reflects reality.
    """
    await put_job(redis_client, _job("old", "111", "published", created=100.0))
    await put_job(redis_client, _job("new", "111", "failed", created=200.0))
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))

    files = client.get(URL).json()["files"]

    assert len(files) == 1, "one Canvas file must not produce two rows"
    assert files[0]["status"] == "published"


@pytest.mark.asyncio
async def test_decisions_sort_first_and_carry_an_action(client, redis_client):  # noqa: ANN001
    await put_job(redis_client, _job("j1", "111", "published", created=100.0))
    await put_job(redis_client, _job("j2", "222", "awaiting_approval", created=101.0))
    await put_job(redis_client, _job("j3", "333", "processing", created=102.0))
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))

    body = client.get(URL).json()
    files = body["files"]

    assert files[0]["bucket"] == "needs_you", (
        "the screen promises decisions first; a finished document must not "
        "outrank something waiting on the reader"
    )
    assert files[-1]["bucket"] == "done", "finished work sinks to the bottom"
    needs = [f for f in files if f["bucket"] == "needs_you"]
    assert len(needs) == 1
    assert needs[0]["action_url"].endswith("/pii"), (
        "a privacy gate must route to the privacy screen, not the a11y one"
    )
    assert body["counts"]["needs_you"] == 1
    assert body["counts"]["converting"] == 1


@pytest.mark.asyncio
async def test_broken_documents_explain_themselves(client, redis_client):  # noqa: ANN001
    """``page_failed`` is the one faculty hit most and understand least.

    The document converted fine; Canvas refused the page write. Saying
    "did not convert" would send them chasing the wrong thing.
    """
    await put_job(redis_client, _job("j1", "111", "page_failed", created=100.0))
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))

    row = client.get(URL).json()["files"][0]

    assert row["bucket"] == "attention"
    assert "locked" in row["reason"].lower()
    assert "converted successfully" in row["reason"].lower()


@pytest.mark.asyncio
async def test_other_courses_are_not_listed(client, redis_client):  # noqa: ANN001
    """Course scoping is a privacy boundary, not a convenience."""
    await put_job(redis_client, _job("j1", "111", "published", created=100.0))
    await put_job(
        redis_client, _job("j2", "999", "published", created=100.0, course="other")
    )
    client.cookies.set(SESSION_COOKIE, await _session(redis_client))

    files = client.get(URL).json()["files"]

    assert [f["canvas_file_id"] for f in files] == ["111"]
