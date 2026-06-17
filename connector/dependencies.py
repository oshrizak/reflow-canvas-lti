"""Dependency-injection helpers shared by connector routes and workers.

Currently provides the Redis client used by the LTI module (and, after
Phase D-F, the Canvas modules + workers). Kept intentionally small: the
upstream Reflow Core ``dependencies.py`` also wires S3 and several
domain services, but those concerns live in core — the connector only
needs Redis here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as redis

from .config import settings

# Singleton Redis connection pool reused across requests so each call
# doesn't pay the TCP-connect cost.
_redis_pool: redis.ConnectionPool | None = None


def _get_redis_pool() -> redis.ConnectionPool:
    """Return the process-wide singleton Redis pool, creating it on first use."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
        )
    return _redis_pool


async def get_redis_client() -> AsyncGenerator[Any, None]:
    """FastAPI dependency: yield a Redis client from the shared pool.

    The client returns to the pool automatically when the request scope
    ends — no explicit close needed.
    """
    client = redis.Redis(connection_pool=_get_redis_pool())
    yield client
