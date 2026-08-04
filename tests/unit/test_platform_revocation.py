"""Soft-revocation invariants for PlatformInstall.

``mark_revoked()`` has existed since the multi-tenant work landed, but
until the launch gate in ``connector/lti/routes.py`` was added nothing
ever read ``revoked_at`` — revoking a platform was a no-op and the
platform kept launching.

The gate reads the value returned by ``put_platform()``. These tests pin
the two properties it depends on:

  1. ``mark_revoked`` persists ``revoked_at``.
  2. A subsequent *launch-shaped* upsert does not clear it. This is the
     one that actually matters: every launch rebuilds a fresh
     ``PlatformInstall`` from the JWT (``revoked_at=None``) and upserts
     it, so if the merge dropped the marker, revocation would silently
     lift itself on the next launch.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio

from connector.lti.platform import build_install_from_launch
from connector.lti.platform_store import (
    clear_revoked,
    get_platform,
    mark_revoked,
    put_platform,
)

ISSUER = "https://canvas.instructure.com"
CLIENT_ID = "10000000000042"
DEPLOYMENT_ID = "4242:abcdef0123456789"


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


def _launch_install():
    """A fresh record exactly as a real launch would build it."""
    return build_install_from_launch(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        deployment_id=DEPLOYMENT_ID,
        label="Canvas - canvas",
    )


@pytest.mark.asyncio
async def test_fresh_install_is_not_revoked(redis):
    install = await put_platform(redis, _launch_install())
    assert install.revoked_at is None


@pytest.mark.asyncio
async def test_mark_revoked_persists(redis):
    install = await put_platform(redis, _launch_install())

    assert await mark_revoked(redis, install.platform_id) is True

    stored = await get_platform(redis, install.platform_id)
    assert stored is not None
    assert stored.revoked_at


@pytest.mark.asyncio
async def test_relaunch_does_not_lift_revocation(redis):
    """The regression this guards: revocation must survive a relaunch.

    A launch builds a brand-new install with ``revoked_at=None``. If the
    upsert merge let that overwrite the stored marker, a revoked platform
    would quietly un-revoke itself the next time anyone opened the tool.
    """
    first = await put_platform(redis, _launch_install())
    await mark_revoked(redis, first.platform_id)

    # Simulate the next LTI launch: same platform, fresh record.
    relaunched = await put_platform(redis, _launch_install())

    assert relaunched.platform_id == first.platform_id
    assert relaunched.revoked_at, (
        "revoked_at was cleared by a relaunch — the launch gate in "
        "lti/routes.py reads this value, so losing it re-enables a "
        "revoked platform"
    )


@pytest.mark.asyncio
async def test_clear_revoked_lifts_it(redis):
    install = await put_platform(redis, _launch_install())
    await mark_revoked(redis, install.platform_id)

    assert await clear_revoked(redis, install.platform_id) is True

    stored = await get_platform(redis, install.platform_id)
    assert stored is not None
    assert stored.revoked_at is None


@pytest.mark.asyncio
async def test_mark_revoked_unknown_platform_returns_false(redis):
    assert await mark_revoked(redis, "does-not-exist") is False
