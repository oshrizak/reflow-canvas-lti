"""Deleting a PDF in Canvas must erase Reflow's memory of it.

Faculty who remove a document expect it gone. Before ``purge_job`` the
record outlived its source: the review screen kept listing a row nobody
could act on, and the bridge kept retrying a document that no longer
existed — in one live case producing a 403 against a deleted page every
thirty seconds indefinitely.

Erasing the job record alone is not enough, which is what these tests
pin. Two of the four keys are easy to forget and both cause quiet
misbehaviour later: a surviving *processed* marker means a re-upload of
the same file is never picked up again, and a surviving *page mapping*
aims the next conversion at a page belonging to a deleted document.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from connector.canvas.state import (
    CanvasJob,
    get_file_page,
    get_job,
    list_pending,
    mark_processed,
    purge_job,
    put_file_page,
    put_job,
)

COURSE = "50594"
FILE_ID = "7275464"
JOB_ID = "job-abc"


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


def _job(status: str = "awaiting_review") -> CanvasJob:
    return CanvasJob(
        reflow_job_id=JOB_ID,
        canvas_file_id=FILE_ID,
        canvas_file_name="01 SPRITE Chimera Student Module_pdf.pdf",
        canvas_course_id=COURSE,
        canvas_user_id="u1",
        status=status,
        created_at=100.0,
    )


@pytest.mark.asyncio
async def test_purge_removes_the_record(redis):
    await put_job(redis, _job())
    assert await get_job(redis, JOB_ID) is not None

    await purge_job(redis, _job())

    assert await get_job(redis, JOB_ID) is None


@pytest.mark.asyncio
async def test_purge_clears_the_review_queue_entry(redis):
    """An awaiting_review job sits in the pending set.

    Leave it there and the review screen shows a row whose job record is
    already gone.
    """
    await put_job(redis, _job("awaiting_review"))
    assert len(await list_pending(redis, COURSE)) == 1

    await purge_job(redis, _job("awaiting_review"))

    assert await list_pending(redis, COURSE) == []


@pytest.mark.asyncio
async def test_purge_frees_the_file_for_reconversion(redis):
    """The processed marker is what stops a second conversion.

    If it survives the purge, re-uploading the same document is silently
    ignored forever — the worst kind of bug, because nothing errors.
    """
    await mark_processed(redis, COURSE, FILE_ID)

    await purge_job(redis, _job())

    from connector.canvas.state import already_processed

    assert await already_processed(redis, COURSE, FILE_ID) is False


@pytest.mark.asyncio
async def test_purge_forgets_the_canvas_page_mapping(redis):
    """Otherwise the next conversion writes into the deleted document's page."""
    await put_file_page(redis, COURSE, FILE_ID, "some-slug")

    await purge_job(redis, _job())

    assert await get_file_page(redis, COURSE, FILE_ID) is None


@pytest.mark.asyncio
async def test_purge_leaves_other_documents_alone(redis):
    """A purge is surgical. Deleting one PDF must not disturb the course."""
    other = CanvasJob(
        reflow_job_id="job-other",
        canvas_file_id="999",
        canvas_file_name="untouched.pdf",
        canvas_course_id=COURSE,
        canvas_user_id="u1",
        status="published",
        created_at=100.0,
    )
    await put_job(redis, other)
    await mark_processed(redis, COURSE, "999")
    await put_job(redis, _job())

    await purge_job(redis, _job())

    assert await get_job(redis, "job-other") is not None

    from connector.canvas.state import already_processed

    assert await already_processed(redis, COURSE, "999") is True
