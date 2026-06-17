"""Thin async client for the Reflow REST API.

Paths and response shapes mirror what ``src/api/documents.py`` actually
exposes today:

  * ``POST /api/v1/documents/submit`` - upload a document, returns ``{job_id}``
  * ``GET  /api/v1/documents/{job_id}`` - status; when status == ``completed``
    the same payload includes ``markdown_url`` (S3 presigned) and
    ``stored_figures``.

We could call into the in-process services directly because the
integration lives in the same monolith, but going over HTTP keeps the
seam clean and matches what an external deployment looks like.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings
from ..utils.retry_helpers import retry_with_backoff

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

# Same retry posture as the Canvas client. The Reflow API is internal in
# production but still rides over HTTP, so transient 502s during a
# rolling deploy or a gateway hiccup shouldn't kill the watcher tick.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 30.0


# Mime types for the document formats Reflow accepts. The pipeline backend
# delegates parsing to Docling, which is format-agnostic - this map only
# exists so multipart uploads carry a sensible Content-Type header.
_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".html": "text/html",
    ".htm": "text/html",
    ".epub": "application/epub+zip",
}


def _mime_for_filename(filename: str) -> str:
    lower = filename.lower()
    for ext, mime in _MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"


class ReflowClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or _default_base_url()).rstrip("/")
        key = api_key or _default_api_key()
        self._headers = {"X-API-Key": key} if key else {}
        self._timeout = timeout

    async def submit_document(
        self,
        filename: str,
        file_bytes: bytes,
        *,
        review_mode: str = "human",
        skip_pii_scan: bool = False,
    ) -> str:
        """Submit any supported document for processing. Returns the Reflow job id.

        Supported types: PDF, DOCX, PPTX (whatever Docling handles). Mime type
        is inferred from the filename extension; falls back to octet-stream so
        Docling's own sniffer can take over.

        ``review_mode="human"`` is the default - the converted output lands in
        the awaiting_review queue so faculty must approve before students can
        see the alt formats.
        """

        url = f"{self.base_url}/api/v1/documents/submit"
        mime = _mime_for_filename(filename)
        files = {"file": (filename, file_bytes, mime)}
        data: dict[str, str] = {"review_mode": review_mode}
        if skip_pii_scan:
            data["skip_pii_scan"] = "true"
            data["skip_reason"] = "Canvas-uploaded course material"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=self._headers, files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
            return payload["job_id"]

    # Backward-compatible alias. New code should use submit_document.
    async def submit_pdf(
        self,
        filename: str,
        pdf_bytes: bytes,
        *,
        review_mode: str = "human",
        skip_pii_scan: bool = False,
    ) -> str:
        return await self.submit_document(
            filename, pdf_bytes, review_mode=review_mode, skip_pii_scan=skip_pii_scan,
        )

    async def get_status(self, job_id: str) -> dict[str, Any]:
        """Return the full status payload.

        When ``status == "completed"`` the payload also carries the result
        URLs (``markdown_url``, ``stored_figures``, ``bundle_url``). The
        bridge worker calls this on every tick for every in-flight job,
        so retries here directly affect whether faculty see stalled dials
        after a transient blip.
        """

        url = f"{self.base_url}/api/v1/documents/{job_id}"

        async def _go() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers)
                resp.raise_for_status()
                return resp.json()

        return await retry_with_backoff(
            _go,
            max_attempts=_RETRY_MAX_ATTEMPTS,
            base_delay=_RETRY_BASE_DELAY,
            max_delay=_RETRY_MAX_DELAY,
            operation_name=f"reflow.get_status({job_id})",
        )

    async def fetch_markdown(self, markdown_url: str) -> str:
        """Pull markdown content from the presigned S3 URL Reflow returned.

        ``markdown_url`` is an S3 presigned GET - no auth header needed,
        and we deliberately do not send our X-API-Key (S3 doesn't speak it).

        The URL is rewritten to the internal S3 hostname (see
        ``rewrite_presigned_url``) so that server-to-server fetches in
        Docker dev work even when the presigner returned a public host
        like ``localhost:4566``. In production with real AWS S3, the
        rewrite is a no-op.
        """

        fetch_url = rewrite_presigned_url(markdown_url)

        async def _go() -> str:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(fetch_url, follow_redirects=True)
                resp.raise_for_status()
                return resp.text

        return await retry_with_backoff(
            _go,
            max_attempts=_RETRY_MAX_ATTEMPTS,
            base_delay=_RETRY_BASE_DELAY,
            max_delay=_RETRY_MAX_DELAY,
            operation_name="reflow.fetch_markdown",
        )


def rewrite_presigned_url(url: str) -> str:
    """Swap an S3 presigned URL's public hostname for the internal one.

    In dev, an S3-compatible API generates presigned URLs against its
    public hostname (e.g. ``localhost:4566``) so browsers can hit them.
    But server-side code inside the Docker network can't reach
    ``localhost:4566`` - that's the container's own loopback. The
    internal hostname is ``floci:4566``. This function rewrites only the
    host portion of the URL, leaving the path and query string (which
    carry the SigV4 signature) untouched.

    No-op when either setting is unset or they're equal - which is the
    expected production posture with real AWS S3.
    """
    public = (getattr(settings, "s3_public_url", None) or "").rstrip("/")
    internal = (getattr(settings, "s3_internal_url", None) or "").rstrip("/")
    if not public or not internal or public == internal:
        return url
    if url.startswith(public + "/") or url == public:
        return internal + url[len(public):]
    return url


def _default_base_url() -> str:
    return getattr(settings, "reflow_api_url", "http://api-gateway:8080") or "http://api-gateway:8080"


def _default_api_key() -> str:
    raw = getattr(settings, "api_keys", None)
    if raw is None:
        return ""
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    first = str(raw).split(",", 1)[0].strip()
    return first
