"""Shared integration-test fixtures.

* ``redis_client``: a fresh ``fakeredis.asyncio.FakeRedis`` per test.
* ``app``: the real FastAPI app with the redis dependency overridden
  to use the in-memory fake.
* ``client``: a ``starlette.testclient.TestClient`` bound to the app.
* ``instructor_session``: writes a real ``SessionPayload`` into Redis
  and returns the cookie value + user id so tests can drive
  authenticated requests without touching the LTI flow.
* ``csrf_header``: produces the X-CSRF-Token header value bound to
  the session id.

Reflow Core / Canvas outbound HTTP is mocked per-test with respx.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from connector.dependencies import get_redis_client
from connector.lti.session import SessionPayload, new_session_id, put_session
from starlette.testclient import TestClient


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """Each test gets a virgin in-memory Redis. Keys do not survive
    between tests — that's the whole point."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def app(redis_client):  # noqa: ANN001 — fakeredis type leaks; not test contract
    """Real FastAPI app with the redis dependency overridden.

    Imported lazily inside the fixture so test collection doesn't
    blow up if the import chain breaks elsewhere — the failure shows
    up against the individual test instead of every test in the file.
    """
    from connector.main import app as real_app

    async def _override_redis():
        yield redis_client

    real_app.dependency_overrides[get_redis_client] = _override_redis
    yield real_app
    real_app.dependency_overrides.pop(get_redis_client, None)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # noqa: ANN001
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture
async def instructor_session(redis_client) -> dict[str, str]:  # noqa: ANN001
    """Plant an instructor LTI session in Redis and hand back the
    cookie + user id. Course id is fixed to ``"c1"`` unless callers
    need otherwise (rare).

    Returns a dict with ``session_id``, ``user_id``, ``course_id``,
    ``user_name``, ``user_email`` — convenient destructuring in tests.
    """
    sid = new_session_id()
    sess = SessionPayload(
        user_id="instructor-1",
        user_name="Pat Faculty",
        user_email="pat@example.edu",
        course_id="c1",
        roles=[
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
        ],
    )
    await put_session(redis_client, sid, sess)
    return {
        "session_id": sid,
        "user_id": sess.user_id,
        "course_id": sess.course_id,
        "user_name": sess.user_name or "",
        "user_email": sess.user_email or "",
    }


@pytest.fixture
def csrf_header(instructor_session) -> dict[str, str]:  # noqa: ANN001
    """``X-CSRF-Token`` value bound to the planted instructor session.

    Importing the helper inline keeps this fixture from forcing the
    whole canvas_panorama module to load at collection time.
    """
    from connector.api.canvas_panorama import _csrf_token_for

    return {"X-CSRF-Token": _csrf_token_for(instructor_session["session_id"])}


@pytest.fixture
def trusted_origin_headers() -> dict[str, str]:
    """Headers state-changing requests need to clear
    ``_require_trusted_origin``. Without an Origin (or matching Referer)
    on the allowed list, the handler 403s before the business logic
    runs. Tests post-mount this onto every state-changing request.
    """
    from connector.config import settings

    allowed = (settings.canvas_allowed_origins or "").split(",")
    origin = (allowed[0].strip() if allowed and allowed[0].strip() else "https://canvas.instructure.com").rstrip("/")
    return {"Origin": origin, "Referer": origin + "/"}


# Helper for tests that need to inject a CanvasJob into Redis.
@dataclasses.dataclass
class _PlantedJob:
    job_id: str
    course_id: str


@pytest_asyncio.fixture
async def planted_job(redis_client, instructor_session) -> _PlantedJob:  # noqa: ANN001
    """Write a minimal awaiting_review CanvasJob into Redis so the
    approve / reject / pii-decision handlers have something to operate
    on. Returns a small dataclass for ergonomic destructuring."""
    from connector.canvas.state import CanvasJob, put_job

    job = CanvasJob(
        reflow_job_id="job-1",
        canvas_file_id="f-1",
        canvas_file_name="doc.pdf",
        canvas_course_id=instructor_session["course_id"],
        canvas_user_id=instructor_session["user_id"],
        status="awaiting_review",
        created_at=0.0,
    )
    await put_job(redis_client, job)
    return _PlantedJob(job_id="job-1", course_id=instructor_session["course_id"])
