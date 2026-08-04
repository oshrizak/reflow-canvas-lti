"""A refreshed token must carry the scopes the user actually consented to.

Canvas does not carry consented scopes forward across a refresh when the
developer key has "Enforce Scopes" enabled — it issues the key's defaults
instead. Omitting ``scope`` on the refresh grant therefore hands back a
progressively weaker token.

That failure is unpleasant to diagnose because it is delayed and it lies.
Everything works for the hour after consent. Then reads start returning
401 and page writes start returning 403, which look like a locked Canvas
page, a missing course permission, or a revoked key — anything except a
refresh that silently dropped half its grant. This module pins the
behaviour so the flow cannot regress into that shape again.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx
from connector.canvas.user_oauth import (
    USER_SCOPES,
    UserOAuthError,
    refresh_user_token,
)
from connector.lti.platform import build_install_from_launch

ISSUER = "https://canvas.instructure.com"


def _platform():
    return build_install_from_launch(
        issuer=ISSUER,
        client_id="10000000000042",
        deployment_id="4242:abcdef",
        label="Canvas",
    )


def _ok_payload() -> dict:
    return {"access_token": "fresh-token", "expires_in": 3600}


def _form(request: httpx.Request) -> dict[str, str]:
    """Decode the posted form body. ``scope`` is percent-encoded on the
    wire (``url:POST|/api/...`` is full of reserved characters), so read
    it back properly rather than string-matching the raw payload."""
    parsed = parse_qs(request.content.decode(), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


@pytest.mark.asyncio
@respx.mock
async def test_refresh_requests_the_consented_scopes():
    platform = _platform()
    route = respx.post(platform.auth_token_url).mock(
        return_value=httpx.Response(200, json=_ok_payload())
    )

    await refresh_user_token(
        platform,
        refresh_token="r1",
        canvas_user_id="101644",
        client_secret="s3cret",
    )

    body = _form(route.calls[0].request)
    assert "scope" in body, (
        "a refresh without scope is how the token quietly loses its grant"
    )

    requested = body["scope"].split(" ")
    assert requested == USER_SCOPES, (
        "the refresh must ask for exactly what consent granted"
    )
    # Named explicitly because these two are what the bridge needs to
    # publish, and losing them is what produced a day of 403s that looked
    # like locked Canvas pages.
    assert "url:POST|/api/v1/courses/:course_id/pages" in requested
    assert "url:PUT|/api/v1/courses/:course_id/pages/:url_or_id" in requested


@pytest.mark.asyncio
@respx.mock
async def test_refresh_preserves_the_existing_refresh_token():
    """Canvas omits ``refresh_token`` on refresh; losing it locks us out."""
    platform = _platform()
    respx.post(platform.auth_token_url).mock(
        return_value=httpx.Response(200, json=_ok_payload())
    )

    token = await refresh_user_token(
        platform,
        refresh_token="r1",
        canvas_user_id="101644",
        client_secret="s3cret",
    )

    assert token.refresh_token == "r1"
    assert token.access_token == "fresh-token"


@pytest.mark.asyncio
@respx.mock
async def test_falls_back_when_the_key_grants_less_than_we_ask_for():
    """Not every install's key carries the full set.

    Canvas answers ``invalid_scope`` rather than trimming the request, so
    asking strictly would take working deployments offline the moment
    their token expired. Retry once without ``scope``: a weaker token
    still beats a dead one, and the operator gets a warning explaining it.
    """
    platform = _platform()
    responses = [
        httpx.Response(400, json={"error": "invalid_scope"}),
        httpx.Response(200, json=_ok_payload()),
    ]
    route = respx.post(platform.auth_token_url).mock(side_effect=responses)

    token = await refresh_user_token(
        platform,
        refresh_token="r1",
        canvas_user_id="101644",
        client_secret="s3cret",
    )

    assert token.access_token == "fresh-token"
    assert len(route.calls) == 2, "the fallback must actually retry"
    assert "scope" in _form(route.calls[0].request)
    assert "scope" not in _form(route.calls[1].request), (
        "the retry is only useful if it drops the scope Canvas rejected"
    )


@pytest.mark.asyncio
@respx.mock
async def test_other_failures_are_not_retried():
    """A bad secret must surface, not be masked by a second attempt."""
    platform = _platform()
    route = respx.post(platform.auth_token_url).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    with pytest.raises(UserOAuthError):
        await refresh_user_token(
            platform,
            refresh_token="r1",
            canvas_user_id="101644",
            client_secret="wrong",
        )

    assert len(route.calls) == 1


def test_one_canonical_scope_list():
    """The authorize leg and the refresh leg must send the same set.

    They used to be separate literals in separate modules, which is how
    the two drifted apart in the first place.
    """
    from connector.api import canvas_oauth

    assert canvas_oauth._USER_SCOPES is USER_SCOPES
