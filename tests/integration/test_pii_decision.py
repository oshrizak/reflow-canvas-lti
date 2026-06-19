"""End-to-end tests for the PII decision flow.

Every previous regression in this flow (5 separate breaks during the
CSUEB pilot) showed up as a 4xx or 5xx that *didn't* exist in a unit
test because the unit tests stub the handler boundary, not the wire
contract with Reflow Core. These cover the wire contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import respx
from httpx import Response


def _reflow_base() -> str:
    """Match whatever ``ReflowClient`` will resolve as base_url."""
    from connector.config import settings
    return getattr(settings, "reflow_api_base_url", "http://localhost:8080").rstrip("/")


def _pause_status_payload(job_id: str, approval_token: str = "tok-xyz") -> dict[str, Any]:
    """Shape of GET /api/v1/documents/{id} when Reflow has paused for PII."""
    return {
        "job_id": job_id,
        "status": "awaiting_approval",
        "approval_token": approval_token,
        "approval_url": f"/api/v1/approval/{approval_token}/decision",
        "pii_findings": [
            {"entity_type": "EMAIL_ADDRESS", "score": 0.95, "text": "<redacted>"},
        ],
    }


def _plant_pii_job(redis_client, sess: dict[str, str], job_id: str) -> None:
    """Inject an ``awaiting_approval`` CanvasJob into Redis. The handler's
    ``_require_instructor`` 404s without one."""
    from connector.canvas.state import CanvasJob, put_job

    async def go():
        await put_job(redis_client, CanvasJob(
            reflow_job_id=job_id,
            canvas_file_id="f-1",
            canvas_file_name="doc.pdf",
            canvas_course_id=sess["course_id"],
            canvas_user_id=sess["user_id"],
            status="awaiting_approval",
            created_at=0.0,
        ))
    asyncio.get_event_loop().run_until_complete(go())


@pytest.mark.integration
@respx.mock(assert_all_called=False)
def test_pii_approve_hits_reflow_approval_endpoint(
    respx_mock, client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    """The connector must call ``/api/v1/approval/{token}/decision`` —
    not the made-up ``/documents/{job}/pii/approve`` we used to send.

    The bug history: connector used to POST to
    ``/api/v1/documents/{job}/pii/approve`` (no such route on Core),
    Core 405'd, connector wrapped as 502, panorama overlay's fetch
    came back as "Failed to fetch". This pins the URL contract.
    """
    cookies = {"reflow_lti_session": instructor_session["session_id"]}
    job_id = "job-pii-1"
    _plant_pii_job(redis_client, instructor_session, job_id)

    base = _reflow_base()
    respx_mock.get(f"{base}/api/v1/documents/{job_id}").mock(
        return_value=Response(200, json=_pause_status_payload(job_id))
    )
    decision_route = respx_mock.post(f"{base}/api/v1/approval/tok-xyz/decision").mock(
        return_value=Response(200, json={
            "message": "Job approved", "job_id": job_id, "decision": "approved",
        })
    )

    resp = client.post(
        f"/canvas/panorama/pii-decision/{job_id}",
        cookies=cookies,
        headers={**csrf_header, **trusted_origin_headers, "Content-Type": "application/json"},
        json={"decision": "approved", "justification": "Names are public bylines"},
    )

    assert resp.status_code == 200, resp.text
    assert decision_route.called, "connector did not POST to the approval-token route"
    sent = decision_route.calls.last.request
    body = sent.read().decode("utf-8")
    assert '"decision"' in body and "approved" in body
    assert "justification" in body
    assert "reviewed_by" in body


@pytest.mark.integration
@respx.mock(assert_all_called=False)
def test_pii_decision_requires_csrf(
    respx_mock, client, instructor_session, redis_client,
):
    """Without an ``X-CSRF-Token`` the handler must 403 — no body forwarded
    to Reflow Core. Prevents a malicious page from forging decisions."""
    cookies = {"reflow_lti_session": instructor_session["session_id"]}
    _plant_pii_job(redis_client, instructor_session, "job-pii-2")

    base = _reflow_base()
    forwarded = respx_mock.post(f"{base}/api/v1/approval/tok-xyz/decision").mock(
        return_value=Response(200, json={})
    )
    resp = client.post(
        "/canvas/panorama/pii-decision/job-pii-2",
        cookies=cookies,
        headers={"Content-Type": "application/json"},
        json={"decision": "approved", "justification": "ten chars or more"},
    )
    assert resp.status_code == 403
    assert not forwarded.called


@pytest.mark.integration
@respx.mock(assert_all_called=False)
def test_pii_decision_409_when_token_already_gone(
    respx_mock, client, instructor_session, csrf_header, trusted_origin_headers, redis_client,
):
    """If the Reflow status payload no longer carries an
    ``approval_token`` (because another tab approved already, or the
    gate expired), surface as 409 — what the panorama overlay shows
    as 'This document already cleared the privacy review.'"""
    cookies = {"reflow_lti_session": instructor_session["session_id"]}
    job_id = "job-pii-3"
    _plant_pii_job(redis_client, instructor_session, job_id)

    base = _reflow_base()
    respx_mock.get(f"{base}/api/v1/documents/{job_id}").mock(return_value=Response(200, json={
        "job_id": job_id, "status": "processing",
    }))
    decision_route = respx_mock.post(f"{base}/api/v1/approval/tok-xyz/decision").mock(
        return_value=Response(200, json={})
    )

    resp = client.post(
        f"/canvas/panorama/pii-decision/{job_id}",
        cookies=cookies,
        headers={**csrf_header, **trusted_origin_headers, "Content-Type": "application/json"},
        json={"decision": "approved", "justification": "ten chars or more"},
    )

    assert resp.status_code == 409
    assert not decision_route.called  # no decision POST forwarded
