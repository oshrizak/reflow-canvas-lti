"""Exception types for the Canvas API client."""

from __future__ import annotations


class CanvasApiError(Exception):
    """Raised when Canvas returns a non-success response.

    Carries the HTTP status, a short message, and Canvas's own
    explanation.

    ``body`` exists because a bare status code is not enough to act on.
    Canvas returns 403 for "you may not create pages", "this page is
    locked", and "your token lacks this scope" alike, and the three call
    for completely different responses. Diagnosing one of those from the
    status code alone produced three wrong conclusions in a single
    afternoon; Canvas had been naming the real reason in the body the
    whole time.

    Truncated deliberately: these payloads are error messages, but the
    connector's rule is never to log user content wholesale, and 300
    characters is more than any Canvas error needs.
    """

    _BODY_LIMIT = 300

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        self.body = (body or "").strip()[: self._BODY_LIMIT]
        detail = f"Canvas API {status_code}: {message}"
        if self.body:
            detail = f"{detail} — {self.body}"
        super().__init__(detail)
        self.status_code = status_code
        self.message = message
