"""Exception types for the Canvas API client."""

from __future__ import annotations


class CanvasApiError(Exception):
    """Raised when Canvas returns a non-success response.

    Carries the HTTP status and a short, log-safe message. The full
    response body is left to the logger; callers should not parse it.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Canvas API {status_code}: {message}")
        self.status_code = status_code
        self.message = message
