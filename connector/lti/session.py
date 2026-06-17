"""Short-lived session state for LTI launches, backed by Redis.

The OIDC handshake needs a place to store the ``state`` nonce between the
``/lti/login`` redirect and the ``/lti/launch`` callback. After launch,
the validated claims also live here so the review UI can read identity
without re-validating the JWT on every request.

All keys are namespaced under ``eq-pdf:canvas:`` to match the rest of the
project's Redis convention.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis

from ..canvas.tenant import tk

from .config import get_lti_settings

logger = logging.getLogger(__name__)

STATE_KEY = tk("canvas:state:{nonce}")
SESSION_KEY = tk("canvas:session:{session_id}")
SESSION_TTL_SECONDS = 8 * 3600

# Cookie name used to carry the LTI session id between the launch
# redirect and subsequent panorama/review/oauth endpoints. Lives here
# so non-routes modules (canvas_oauth, etc.) can import it without
# pulling in the whole LTI routes module.
SESSION_COOKIE = "reflow_lti_session"


@dataclass
class StatePayload:
    """Data persisted across the OIDC redirect."""

    nonce: str
    login_hint: str
    lti_message_hint: str | None
    target_link_uri: str
    issuer: str
    client_id: str


@dataclass
class SessionPayload:
    """Identity established after a successful launch."""

    user_id: str
    user_name: str | None
    user_email: str | None
    course_id: str
    roles: list[str]


def new_state_token() -> str:
    return secrets.token_urlsafe(32)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


async def put_state(redis: Redis, state: StatePayload) -> None:
    cfg = get_lti_settings()
    await redis.set(
        STATE_KEY.format(nonce=state.nonce),
        json.dumps(asdict(state)),
        ex=cfg.state_ttl_seconds,
    )


async def take_state(redis: Redis, nonce: str) -> StatePayload | None:
    """Read and delete the state — single-use is the whole point."""

    key = STATE_KEY.format(nonce=nonce)
    raw: Any = await redis.get(key)
    if raw is None:
        return None
    await redis.delete(key)
    data = json.loads(raw)
    return StatePayload(**data)


async def put_session(redis: Redis, session_id: str, session: SessionPayload) -> None:
    await redis.set(
        SESSION_KEY.format(session_id=session_id),
        json.dumps(asdict(session)),
        ex=SESSION_TTL_SECONDS,
    )


async def get_session(redis: Redis, session_id: str) -> SessionPayload | None:
    raw: Any = await redis.get(SESSION_KEY.format(session_id=session_id))
    if raw is None:
        return None
    return SessionPayload(**json.loads(raw))


async def drop_session(redis: Redis, session_id: str) -> None:
    await redis.delete(SESSION_KEY.format(session_id=session_id))
