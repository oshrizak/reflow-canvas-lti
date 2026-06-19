"""Thin async client for the upstream Reflow REST API.

Endpoints consumed:

  * ``POST /api/v1/documents/submit`` — upload a document, returns ``{job_id}``
  * ``GET  /api/v1/documents/{job_id}`` — status; when status == ``completed``
    the same payload includes ``markdown_url`` (S3 presigned) and figures.
  * ``POST /api/v1/documents/{job_id}/pii/approve`` — record a faculty
    approval of flagged PII so Reflow Core resumes processing.
  * ``POST /api/v1/documents/{job_id}/pii/deny`` — record a denial.

The connector is a CLIENT of these endpoints; it does not import any
Reflow Core internal services. ``connector.config.reflow_api_base_url``
points at the running Reflow Core instance.
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


class ReflowApiError(Exception):
    """Raised when Reflow Core returns an error the caller should surface.

    Distinguishes a missing endpoint (404 — likely the running Reflow Core
    is older than the connector expects) from a regular HTTP failure so
    handlers can craft an operator-actionable message.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


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

    async def submit_pii_decision(
        self,
        job_id: str,
        *,
        decision: str,
        justification: str,
        reviewed_by: str,
    ) -> dict[str, Any]:
        """Forward a faculty PII approve/deny decision to Reflow Core.

        Two upstream surfaces, tried in order:

        1. **By-job-id endpoint** (preferred):
           ``POST /api/v1/documents/{job_id}/pii/{approve,deny}``.
           Single round-trip — the connector already authenticated with
           an API key and already knows ``job_id`` from a prior status
           poll, so requiring an approval-token round-trip is
           redundant. Added to Core in
           `EqualifyEverything/equalify-reflow#142 <https://github.com/EqualifyEverything/equalify-reflow/pull/142>`_.
           Returns 405 Method Not Allowed on Core versions that pre-date
           the PR — we detect that and fall back to (2).

        2. **By-approval-token endpoint** (legacy fallback):
           ``POST /api/v1/approval/{token}/decision``. Requires a prior
           ``GET /api/v1/documents/{job_id}`` to read the
           ``approval_token`` off the status payload. Still works for
           the operator-driven flow (emailed approval links) and is the
           shape Core has had all along.

        Payload shape is identical between the two routes:
        ``{decision, justification, reviewed_by}`` with ``decision``
        being ``"approved"`` or ``"denied"``. Response shape (
        ``{message, job_id, decision}``) is also normalized between the
        two; the PR explicitly aligned the by-job-id endpoint with the
        token endpoint's ``ApprovalResponse`` model so callers don't
        branch on success.

        Raises ``ReflowApiError`` for 4xx/5xx, including:
          * 404 when the job is unknown (either endpoint) OR when the
            token endpoint rejects the token (stale / already used).
          * 409 when the job already advanced past awaiting_approval
            (parallel-tab race). The by-job-id endpoint surfaces this
            directly via a status pre-check; the token endpoint
            surfaces it via the connector-side pre-check below.
        """

        if decision not in ("approved", "denied"):
            raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")

        # ----- (1) by-job-id endpoint (PR #142) ---------------------
        # We POST blind here — no status round-trip — because the
        # endpoint itself does the status pre-check and 409s on race.
        # The body shape matches what Core's ``PIIDecisionByJobIdRequest``
        # Pydantic model declares.
        by_id_path = "approve" if decision == "approved" else "deny"
        by_id_url = (
            f"{self.base_url}/api/v1/documents/{job_id}/pii/{by_id_path}"
        )
        by_id_payload = {
            "justification": justification,
            "reviewed_by": reviewed_by,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            by_id_resp = await client.post(
                by_id_url, headers=self._headers, json=by_id_payload,
            )
        # 405 (or 404 on routers that 404 unknown paths instead of
        # 405'ing) means the endpoints from PR #142 aren't deployed on
        # this Core. Fall through to the token flow rather than
        # surfacing the failure.
        if by_id_resp.status_code not in (404, 405):
            if by_id_resp.status_code == 409:
                raise ReflowApiError(
                    by_id_resp.text or "Job already past awaiting_approval",
                    status_code=409,
                )
            if by_id_resp.is_error:
                raise ReflowApiError(
                    f"Reflow Core PII {by_id_path} returned "
                    f"{by_id_resp.status_code}: {by_id_resp.text[:200]}",
                    status_code=by_id_resp.status_code,
                )
            return by_id_resp.json()

        # ----- (2) by-approval-token endpoint (legacy fallback) ----
        # Fetch the job status to pull the current approval token.
        # The token is single-use + time-limited, so we don't cache it.
        status_url = f"{self.base_url}/api/v1/documents/{job_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            status_resp = await client.get(status_url, headers=self._headers)
        if status_resp.status_code == 404:
            raise ReflowApiError(
                f"Reflow Core has no record of job {job_id}", status_code=404,
            )
        if status_resp.is_error:
            raise ReflowApiError(
                f"Reflow Core status fetch returned "
                f"{status_resp.status_code}: {status_resp.text[:200]}",
                status_code=status_resp.status_code,
            )
        status = status_resp.json()
        approval_token = (
            status.get("approval_token")
            or (status.get("approval") or {}).get("token")
        )
        if not approval_token:
            # The job is no longer awaiting approval — typical race
            # cause is a parallel-tab approval that already moved the
            # job to ``processing``. Surface as 409 so the handler
            # tells faculty the right thing.
            raise ReflowApiError(
                f"Job {job_id} has no current approval token "
                f"(status: {status.get('status')!r}). "
                "It may have already been decided in another tab, "
                "or the gate window may have expired.",
                status_code=409,
            )

        decision_url = (
            f"{self.base_url}/api/v1/approval/{approval_token}/decision"
        )
        payload = {
            "decision": decision,
            "justification": justification,
            "reviewed_by": reviewed_by,
        }

        async def _go() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(decision_url, headers=self._headers, json=payload)
                if resp.status_code == 404:
                    raise ReflowApiError(
                        "Reflow Core rejected the approval token "
                        "(stale or already used).",
                        status_code=404,
                    )
                if resp.status_code == 409:
                    raise ReflowApiError(
                        resp.text or "Job already past awaiting_approval",
                        status_code=409,
                    )
                if resp.is_error:
                    raise ReflowApiError(
                        f"Reflow Core approval decision returned "
                        f"{resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                return resp.json()

        return await retry_with_backoff(
            _go,
            max_attempts=_RETRY_MAX_ATTEMPTS,
            base_delay=_RETRY_BASE_DELAY,
            max_delay=_RETRY_MAX_DELAY,
            operation_name=f"reflow.submit_pii_decision({job_id}, {decision})",
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
    """Where the connector should reach Reflow Core.

    Pulls from ``settings.reflow_api_base_url`` (renamed from the source
    fork's ``reflow_api_url`` so the connector reads its own setting name).
    """
    return (
        getattr(settings, "reflow_api_base_url", "http://localhost:8080")
        or "http://localhost:8080"
    )


def _default_api_key() -> str:
    """Read the bearer key the connector sends to Reflow Core.

    Renamed from the source fork's ``api_keys`` (which was a generic
    comma-separated allowlist for *inbound* clients) to ``reflow_api_key``
    so it's unambiguous which direction the key authenticates.
    """
    raw = getattr(settings, "reflow_api_key", None)
    if raw is None:
        return ""
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    return str(raw).strip()
