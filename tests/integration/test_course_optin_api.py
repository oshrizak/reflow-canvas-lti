"""The course opt-in switch, end to end through the real app.

"Off by default" is only safe if there's a way to turn it on that isn't a
Redis command. These drive the endpoints the review page's toggle calls.

The authorisation check is the point: a student launching the tool must not
be able to authorise processing of an entire course.
"""

from __future__ import annotations

import pytest

from connector.lti.routes import SESSION_COOKIE
from connector.lti.session import SessionPayload, new_session_id, put_session

INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
COURSE = "50594"
URL = f"/canvas/review/api/optin?course_id={COURSE}"


async def _session(redis, roles: list[str]) -> str:
    sid = new_session_id()
    await put_session(
        redis,
        sid,
        SessionPayload(
            user_id="u1",
            user_name="Zach",
            user_email="zach@csueastbay.edu",
            course_id=COURSE,
            roles=roles,
        ),
    )
    return sid


@pytest.mark.asyncio
async def test_course_starts_disabled(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    body = client.get(URL).json()

    assert body["enabled"] is False, "a course must not be scanned until asked"
    assert body["can_change"] is True
    assert body["drop_folder"], "faculty need to be told the folder name"


@pytest.mark.asyncio
async def test_instructor_can_enable_and_disable(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))

    assert client.post(URL, json={"enabled": True}).status_code == 200
    after = client.get(URL).json()
    assert after["enabled"] is True
    assert after["enabled_by"] == "zach@csueastbay.edu", (
        "who authorised processing must be recorded, not anonymous"
    )
    assert after["enabled_at"] > 0

    assert client.post(URL, json={"enabled": False}).status_code == 200
    assert client.get(URL).json()["enabled"] is False


@pytest.mark.asyncio
async def test_student_cannot_change_it(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [LEARNER]))

    assert client.get(URL).json()["can_change"] is False
    assert client.post(URL, json={"enabled": True}).status_code == 403


@pytest.mark.asyncio
async def test_student_cannot_undo_an_instructor_decision(client, redis_client):
    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [INSTRUCTOR]))
    client.post(URL, json={"enabled": True})

    client.cookies.set(SESSION_COOKIE, await _session(redis_client, [LEARNER]))
    assert client.post(URL, json={"enabled": False}).status_code == 403
    assert client.get(URL).json()["enabled"] is True


@pytest.mark.asyncio
async def test_requires_a_session(client):
    client.cookies.clear()
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"enabled": True}).status_code == 401
