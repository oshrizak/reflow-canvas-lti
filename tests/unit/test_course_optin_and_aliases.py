"""Per-course opt-in, content-hash dedup, and copy aliasing.

Three behaviours introduced when conversion moved from "scan everything"
to "faculty choose":

  1. ``is_course_enabled`` gates full-course scanning. Off by default, so a
     course is never swept until a human turns it on.
  2. ``get/put_job_for_hash`` recognises byte-identical content so a re-drop
     of the same PDF doesn't pay for a second conversion.
  3. ``get/put_file_alias`` points a copy's Canvas file id at the job that
     converted the original. Faculty copy PDFs into the drop folder, so the
     file students actually click has a different id from the one converted;
     without the alias the accessible version never surfaces where it's
     needed.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from connector.canvas.state import (
    get_course_optin,
    get_file_alias,
    get_job_for_hash,
    is_course_enabled,
    put_file_alias,
    put_job_for_hash,
    set_course_enabled,
)

COURSE = "50594"
SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


# --- 1. course opt-in --------------------------------------------------


@pytest.mark.asyncio
async def test_course_is_disabled_by_default(redis):
    """The safe default. Nothing is swept until someone says so."""
    assert await is_course_enabled(redis, COURSE) is False


@pytest.mark.asyncio
async def test_enabling_records_who_and_when(redis):
    await set_course_enabled(redis, COURSE, enabled=True, actor="pat@example.edu")

    assert await is_course_enabled(redis, COURSE) is True

    record = await get_course_optin(redis, COURSE)
    assert record is not None
    assert record["actor"] == "pat@example.edu"
    assert record["at"] > 0, "an ISO asking 'who authorised this' needs a timestamp"


@pytest.mark.asyncio
async def test_opt_in_is_reversible(redis):
    await set_course_enabled(redis, COURSE, enabled=True, actor="zach")
    await set_course_enabled(redis, COURSE, enabled=False)

    assert await is_course_enabled(redis, COURSE) is False
    assert await get_course_optin(redis, COURSE) is None


@pytest.mark.asyncio
async def test_opt_in_is_per_course(redis):
    await set_course_enabled(redis, COURSE, enabled=True, actor="zach")

    assert await is_course_enabled(redis, "99999") is False, (
        "enabling one course must not enable another"
    )


# --- 2. content-hash dedup --------------------------------------------


@pytest.mark.asyncio
async def test_unknown_hash_returns_none(redis):
    assert await get_job_for_hash(redis, COURSE, SHA) is None


@pytest.mark.asyncio
async def test_hash_round_trip(redis):
    await put_job_for_hash(redis, COURSE, SHA, "job-abc")
    assert await get_job_for_hash(redis, COURSE, SHA) == "job-abc"


@pytest.mark.asyncio
async def test_hash_index_is_scoped_per_course(redis):
    """The same PDF in two courses converts once per course.

    Jobs carry course-specific Canvas page and file ids, so reusing another
    course's job would publish into the wrong course.
    """
    await put_job_for_hash(redis, COURSE, SHA, "job-abc")
    assert await get_job_for_hash(redis, "99999", SHA) is None


# --- 3. copy aliasing --------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_alias_returns_none(redis):
    assert await get_file_alias(redis, COURSE, "7347394") is None


@pytest.mark.asyncio
async def test_alias_round_trip(redis):
    await put_file_alias(redis, COURSE, "7347394", "job-abc")
    assert await get_file_alias(redis, COURSE, "7347394") == "job-abc"


@pytest.mark.asyncio
async def test_several_copies_can_share_one_job(redis):
    """A PDF linked from Files, a Module and a Page has three ids.

    All of them should resolve to the single conversion, so the badge shows
    up wherever a student meets the document.
    """
    for fid in ("111", "222", "333"):
        await put_file_alias(redis, COURSE, fid, "job-abc")

    for fid in ("111", "222", "333"):
        assert await get_file_alias(redis, COURSE, fid) == "job-abc"


@pytest.mark.asyncio
async def test_alias_is_scoped_per_course(redis):
    await put_file_alias(redis, COURSE, "7347394", "job-abc")
    assert await get_file_alias(redis, "99999", "7347394") is None
