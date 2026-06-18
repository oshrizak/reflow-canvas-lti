"""Async Canvas REST client with dual auth modes.

Three construction paths:

  * ``CanvasClient(base_url=..., api_token=...)`` -- legacy single-tenant
    path. Bearer comes from ``settings.canvas_api_token``. Kept for the
    watcher in non-multi-tenant deployments and for local dev.

  * ``CanvasClient.from_platform(redis, platform, scopes)`` -- LTI
    Advantage service-token path (Phase 3). Bearer is minted via
    ``canvas.oauth.get_service_token`` against the platform's token
    endpoint. Canvas Cloud only honors these tokens at LTI Advantage
    service endpoints (NRPS, AGS, Deep Linking) -- they are NOT valid
    against the general ``/api/v1/...`` REST API.

  * ``CanvasClient.from_user_token(redis, platform, user_id)`` -- per-
    user OAuth2 path (Phase 8). Bearer is a user-bound token obtained
    through Canvas's authorization-code flow with explicit faculty
    consent. This is the path that actually works for the general
    Canvas REST API in Canvas Cloud. Refreshes silently on 401 via the
    stored refresh_token.

Both paths converge through ``_headers()``, an async helper that
produces the Authorization dict for every API call. Method bodies do
not know or care which auth mode is active.

401 handling: when a call returns 401 and we're in platform mode, the
cached token is invalidated and the call is retried exactly once with a
fresh token. The retry covers both "Canvas rotated its own key" and
"the previous token aged out faster than expected"; a second 401 after
the refresh is surfaced to the caller as a permission problem.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..config import settings
from ..utils.retry_helpers import retry_with_backoff
from .errors import CanvasApiError

# Canvas occasionally returns 429 / 502 / 503 under load (especially during
# bulk roster pulls or the start of a semester). Wrap network calls in
# exponential backoff so a single transient blip doesn't kill the watcher
# tick. Numbers are tuned to be patient (Canvas's 429 rate-limit window is
# ~3s) but not stalling (max 30s of waiting per call). Caller-visible
# permanent errors (404, 401) are *not* retried - the helper categorizes
# via is_retryable_error.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 30.0

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class CanvasClient:
    """Async client scoped to a single Canvas instance.

    Construction is cheap; create one per request or share one process-wide
    -- ``httpx.AsyncClient`` is safe to share.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or getattr(settings, "canvas_api_url", "") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("CanvasClient: base_url is required")
        token = api_token or _resolve_default_token()
        self._static_headers: dict[str, str] | None = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout
        # Platform-mode fields stay None on the legacy path.
        self._redis: Any | None = None
        self._platform: Any | None = None
        self._scopes: list[str] | None = None
        # User-OAuth-mode fields stay None on the other two paths.
        self._user_id: str | None = None

    @classmethod
    def from_platform(
        cls,
        redis: Any,
        platform: Any,
        scopes: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CanvasClient:
        """Construct a client that authenticates via LTI service tokens.

        ``platform`` is a ``PlatformInstall`` from ``connector.lti.platform``.
        ``scopes`` is the list of Canvas API scopes this client will
        request -- typically the union of scopes the surrounding code
        path needs. Tokens are cached per (platform, scope-set) so two
        callers asking for the same scope set share a bearer.
        """

        inst = cls.__new__(cls)
        api_base = (platform.canvas_api_base or "").rstrip("/")
        # The legacy code does f"{self.base_url}/api/v1/...", so base_url
        # is the bare scheme+host. The platform's canvas_api_base ends in
        # /api/v1, so strip it off.
        if api_base.endswith("/api/v1"):
            inst.base_url = api_base[: -len("/api/v1")]
        else:
            inst.base_url = api_base
        if not inst.base_url:
            raise ValueError("CanvasClient.from_platform: platform.canvas_api_base is empty")
        inst._timeout = timeout
        inst._static_headers = None
        inst._redis = redis
        inst._platform = platform
        inst._scopes = list(scopes)
        inst._user_id = None
        return inst

    @classmethod
    def from_user_token(
        cls,
        redis: Any,
        platform: Any,
        user_id: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CanvasClient:
        """Construct a client that uses a faculty member's OAuth2 token.

        ``platform`` is a ``PlatformInstall``; ``user_id`` is the
        ``lti.session.SessionPayload.user_id`` (i.e. Canvas's ``sub``
        claim). The user must have completed the OAuth2 consent flow
        beforehand; this constructor does not initiate it.

        On every call, the client loads the stored token from Redis,
        checks expiry, refreshes via the stored refresh_token if needed,
        and uses the resulting access_token as the Bearer credential.
        401s trigger a forced refresh + one retry, same shape as
        ``from_platform``.
        """
        inst = cls.__new__(cls)
        api_base = (platform.canvas_api_base or "").rstrip("/")
        if api_base.endswith("/api/v1"):
            inst.base_url = api_base[: -len("/api/v1")]
        else:
            inst.base_url = api_base
        if not inst.base_url:
            raise ValueError(
                "CanvasClient.from_user_token: platform.canvas_api_base empty"
            )
        inst._timeout = timeout
        inst._static_headers = None
        inst._redis = redis
        inst._platform = platform
        inst._scopes = None
        inst._user_id = user_id
        return inst

    # ---- Auth ----------------------------------------------------------

    async def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        """Build the Authorization header for one API call.

        Three paths converge here:

        * Static (legacy): returns the dict computed in ``__init__``.
        * Platform (service-token): asks ``canvas.oauth`` for a cached or
          fresh service token.
        * User-OAuth: loads the user's stored access_token, refreshes
          via the stored refresh_token if expired or if
          ``force_refresh`` is set.
        """
        if self._static_headers is not None:
            return dict(self._static_headers)
        if self._user_id is not None:
            return await self._user_token_headers(force_refresh=force_refresh)
        # Platform/service-token path
        from .oauth import get_service_token

        token = await get_service_token(
            self._redis,
            self._platform,
            self._scopes or [],
            force_refresh=force_refresh,
        )
        return {"Authorization": f"Bearer {token.access_token}"}

    async def _user_token_headers(self, *, force_refresh: bool) -> dict[str, str]:
        """Headers from the user-OAuth path: load, refresh-if-stale, return."""
        from .user_oauth import (
            UserOAuthError,
            drop_user_token,
            get_user_token,
            put_user_token,
            refresh_user_token,
        )

        token = await get_user_token(
            self._redis, self._platform.platform_id, self._user_id
        )
        if token is None:
            raise CanvasApiError(
                401,
                f"No user OAuth token stored for user_id={self._user_id} "
                f"on platform={self._platform.platform_id}. The user has "
                "not yet completed the Canvas authorize flow.",
            )

        if force_refresh or token.is_expired():
            if not token.refresh_token:
                # No refresh token -> can't silently renew. Bubble up as
                # 401 so the surrounding LTI flow can re-prompt consent.
                await drop_user_token(
                    self._redis, self._platform.platform_id, self._user_id
                )
                raise CanvasApiError(
                    401,
                    "Stored user token expired and has no refresh_token; "
                    "user must re-authorize via /canvas/oauth/authorize.",
                )
            # Pick client-auth scheme matching whichever dev key the
            # operator configured. See the matching block in
            # src/api/canvas_oauth.py callback handler for the rationale.
            from ..config import settings as _s
            oauth_secret = getattr(_s, "canvas_oauth_client_secret", None)
            secret_val = ""
            if oauth_secret is not None:
                secret_val = (
                    oauth_secret.get_secret_value()
                    if hasattr(oauth_secret, "get_secret_value")
                    else str(oauth_secret)
                )
            try:
                if secret_val:
                    token = await refresh_user_token(
                        self._platform,
                        refresh_token=token.refresh_token,
                        canvas_user_id=token.canvas_user_id,
                        client_secret=secret_val,
                    )
                else:
                    from ..api.canvas_oauth import (
                        _client_assertion_for_token_endpoint,
                    )
                    assertion = _client_assertion_for_token_endpoint(
                        self._platform
                    )
                    token = await refresh_user_token(
                        self._platform,
                        refresh_token=token.refresh_token,
                        canvas_user_id=token.canvas_user_id,
                        client_assertion=assertion,
                    )
            except UserOAuthError as exc:
                logger.warning(
                    "User token refresh failed for user=%s platform=%s: %s",
                    self._user_id, self._platform.platform_id, exc,
                )
                await drop_user_token(
                    self._redis, self._platform.platform_id, self._user_id
                )
                raise CanvasApiError(
                    401,
                    f"User token refresh failed: {exc}; re-authorize needed.",
                ) from exc
            await put_user_token(
                self._redis, self._platform.platform_id, self._user_id, token,
            )
        return {"Authorization": f"Bearer {token.access_token}"}

    async def _invalidate_token(self) -> None:
        """Drop the cached service or user token for the active path.

        No-op in legacy/static mode. The 401 retry path calls this
        before requesting fresh headers.
        """
        if self._static_headers is not None:
            return
        if self._user_id is not None:
            # User-OAuth path: nothing to invalidate; ``_headers`` will
            # force a refresh via the stored refresh_token on the next
            # call (we set force_refresh=True in the retry path).
            return
        from .oauth import invalidate

        await invalidate(
            self._redis,
            self._platform.platform_id,
            self._scopes or [],
        )

    async def _request_with_401_retry(
        self,
        do_request: Callable[[dict[str, str]], Awaitable[httpx.Response]],
        op: str,
    ) -> httpx.Response:
        """Run ``do_request`` once, refresh on 401, run again at most once."""
        headers = await self._headers()
        resp = await do_request(headers)
        if resp.status_code != 401 or self._static_headers is not None:
            return resp
        # Platform-mode 401: invalidate + retry once with fresh token.
        logger.info(
            "Canvas %s -> 401; refreshing service token and retrying once", op,
        )
        await self._invalidate_token()
        headers = await self._headers(force_refresh=True)
        return await do_request(headers)

    # ---- Files ---------------------------------------------------------

    async def list_course_pdfs(self, course_id: str) -> list[dict[str, Any]]:
        """Return PDF files in a course (paginated under the hood)."""

        url = f"{self.base_url}/api/v1/courses/{course_id}/files"
        params = {"content_types[]": "application/pdf", "per_page": "100"}
        return await self._get_paged(url, params)

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        """Fetch a file's metadata by id."""

        url = f"{self.base_url}/api/v1/files/{file_id}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.get(url, headers=headers)

        resp = await self._request_with_401_retry(_do, "GET file metadata")
        self._raise_for_status(resp, "GET file metadata")
        return resp.json()

    async def list_modules(self, course_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/courses/{course_id}/modules"
        return await self._get_paged(url, {"per_page": "100"})

    async def list_module_items(self, course_id: str, module_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/courses/{course_id}/modules/{module_id}/items"
        return await self._get_paged(url, {"per_page": "100"})

    async def list_course_folders(self, course_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/courses/{course_id}/folders"
        return await self._get_paged(url, {"per_page": "100"})

    async def list_folder_files(self, folder_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/folders/{folder_id}/files"
        return await self._get_paged(url, {"per_page": "100"})

    async def download_file(self, file_id: str) -> bytes:
        """Fetch the raw bytes of a Canvas file.

        Wrapped in exponential backoff because large textbooks can be
        100MB+ and Canvas occasionally returns a transient 502 mid-fetch.
        """

        url = f"{self.base_url}/api/v1/files/{file_id}"

        async def _do_download() -> bytes:
            async def _meta(headers: dict[str, str]) -> httpx.Response:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    return await client.get(url, headers=headers)

            meta_resp = await self._request_with_401_retry(_meta, "GET file metadata")
            self._raise_for_status(meta_resp, "GET file metadata")
            meta = meta_resp.json()
            download_url = meta.get("url")
            if not download_url:
                raise CanvasApiError(404, f"File {file_id} has no download URL")

            # The download URL is a presigned S3 link; it doesn't need our
            # bearer (Canvas signs the URL with its own credentials). But
            # we still pass headers to follow Canvas's own conventions in
            # case the API ever changes; the S3 bucket just ignores them.
            async def _bytes(headers: dict[str, str]) -> httpx.Response:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    return await client.get(
                        download_url, headers=headers, follow_redirects=True
                    )

            file_resp = await self._request_with_401_retry(_bytes, "download file")
            self._raise_for_status(file_resp, "download file")
            return file_resp.content

        return await retry_with_backoff(
            _do_download,
            max_attempts=_RETRY_MAX_ATTEMPTS,
            base_delay=_RETRY_BASE_DELAY,
            max_delay=_RETRY_MAX_DELAY,
            operation_name=f"canvas.download_file({file_id})",
        )

    async def upload_course_file(
        self,
        course_id: str,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        folder_path: str = "",
        on_duplicate: str = "overwrite",
    ) -> dict[str, Any]:
        """Upload a file into a course via Canvas's 3-step upload flow.

        1. POST to ``/courses/:id/files`` to get a pre-signed ``upload_url``
           + ``upload_params``.
        2. POST the bytes to that storage URL (no bearer — the params are
           already signed). Canvas/S3 returns either the file JSON (201) or
           a 3xx whose ``Location`` finalizes the upload.
        3. If redirected, GET ``Location`` (with our bearer) to get the
           final file object.

        ``folder_path`` is a course-relative folder (created if missing),
        e.g. ``"Reflow Generated Images"``. Returns the Canvas file object,
        which includes ``id`` and ``url`` (a directly-embeddable link).
        """
        init_url = f"{self.base_url}/api/v1/courses/{course_id}/files"
        init_payload: dict[str, Any] = {
            "name": filename,
            "size": len(content),
            "content_type": content_type,
            "on_duplicate": on_duplicate,
        }
        if folder_path:
            init_payload["parent_folder_path"] = folder_path

        async def _init(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.post(init_url, headers=headers, json=init_payload)

        init_resp = await self._request_with_401_retry(_init, "init file upload")
        self._raise_for_status(init_resp, "init file upload")
        init = init_resp.json()
        upload_url = init.get("upload_url")
        upload_params = init.get("upload_params") or {}
        if not upload_url:
            raise CanvasApiError(502, "Canvas file upload: no upload_url returned")

        # Step 2: post to the storage target. No auth header — upload_params
        # carry the signed credentials. Don't auto-follow the redirect so we
        # can finalize it ourselves with our bearer.
        files = {"file": (filename, content, content_type)}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            store_resp = await client.post(
                upload_url, data=upload_params, files=files, follow_redirects=False
            )

        if store_resp.status_code in (200, 201):
            try:
                return store_resp.json()
            except ValueError:
                pass
        location = store_resp.headers.get("Location")
        if location:
            async def _confirm(headers: dict[str, str]) -> httpx.Response:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    # Canvas wants an empty POST/GET to finalize; GET works.
                    return await client.get(location, headers=headers)

            confirm_resp = await self._request_with_401_retry(_confirm, "confirm file upload")
            self._raise_for_status(confirm_resp, "confirm file upload")
            return confirm_resp.json()
        self._raise_for_status(store_resp, "store file upload")
        return {}

    # ---- Pages ---------------------------------------------------------

    async def create_page(
        self,
        course_id: str,
        title: str,
        body_html: str,
        *,
        published: bool = False,
        notify_of_update: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages"
        payload = {
            "wiki_page": {
                "title": title,
                "body": body_html,
                "published": published,
                "notify_of_update": notify_of_update,
            }
        }

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.post(url, headers=headers, json=payload)

        resp = await self._request_with_401_retry(_do, "create page")
        self._raise_for_status(resp, "create page")
        return resp.json()

    @staticmethod
    def _page_ref(page_url_or_slug: str) -> str:
        """Normalize a page reference to the bare slug Canvas's API expects.

        Accepts either a stored full URL (``https://host/courses/1/pages/my-page``)
        or a bare slug (``my-page``) and returns just ``my-page``. This lets the
        rest of the code store the full ``html_url`` (so the overlay link works)
        while the pages API still gets the ``:url_or_id`` segment it needs.
        """
        s = (page_url_or_slug or "").strip()
        if "/pages/" in s:
            s = s.split("/pages/", 1)[1]
        s = s.split("?", 1)[0].split("#", 1)[0]
        return s.strip("/")

    async def update_page(
        self,
        course_id: str,
        page_ref: str,
        title: str,
        body_html: str,
    ) -> dict[str, Any]:
        """Update an existing wiki page's title + body in place (PUT).

        Raises ``CanvasApiError(404)`` when the page doesn't exist, so the
        bridge can fall back to ``create_page``. Publish state is left
        untouched (we don't send ``published``), so a draft stays a draft.
        """
        ref = self._page_ref(page_ref)
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{ref}"
        payload = {"wiki_page": {"title": title, "body": body_html}}

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.put(url, headers=headers, json=payload)

        resp = await self._request_with_401_retry(_do, "update page")
        self._raise_for_status(resp, "update page")
        return resp.json()

    async def publish_page(self, course_id: str, page_url: str) -> dict[str, Any]:
        ref = self._page_ref(page_url)
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{ref}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.put(
                    url, headers=headers, json={"wiki_page": {"published": True}},
                )

        resp = await self._request_with_401_retry(_do, "publish page")
        self._raise_for_status(resp, "publish page")
        return resp.json()

    async def unpublish_page(self, course_id: str, page_url: str) -> dict[str, Any]:
        """Set a wiki page back to unpublished (draft) so students can't see it.

        Mirror of ``publish_page``: PUTs ``published: false``. Used when faculty
        take an accessible page down from the overlay's Unpublish action.
        """
        ref = self._page_ref(page_url)
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{ref}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.put(
                    url, headers=headers, json={"wiki_page": {"published": False}},
                )

        resp = await self._request_with_401_retry(_do, "unpublish page")
        self._raise_for_status(resp, "unpublish page")
        return resp.json()

    async def delete_page(self, course_id: str, page_url: str) -> None:
        ref = self._page_ref(page_url)
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{ref}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.delete(url, headers=headers)

        resp = await self._request_with_401_retry(_do, "delete page")
        self._raise_for_status(resp, "delete page")

    # ---- Conversations ------------------------------------------------

    async def send_conversation(
        self,
        recipient_user_id: str,
        subject: str,
        body: str,
        *,
        context_code: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/conversations"
        payload: dict[str, Any] = {
            "recipients[]": recipient_user_id,
            "subject": subject,
            "body": body,
        }
        if context_code:
            payload["context_code"] = context_code

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.post(url, headers=headers, data=payload)

        resp = await self._request_with_401_retry(_do, "send conversation")
        self._raise_for_status(resp, "send conversation")
        return resp.json()

    # ---- Content surfaces (scan for embedded file references) --------

    async def list_pages(self, course_id: str) -> list:
        url = f"{self.base_url}/api/v1/courses/{course_id}/pages"
        return await self._get_paged(url, {"per_page": "100", "published": "true"})

    async def get_page(self, course_id: str, page_url: str) -> dict:
        full_url = f"{self.base_url}/api/v1/courses/{course_id}/pages/{page_url}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.get(full_url, headers=headers)

        resp = await self._request_with_401_retry(_do, "GET page")
        self._raise_for_status(resp, "GET page")
        return resp.json()

    async def list_discussion_topics(self, course_id: str, include_announcements: bool = True) -> list:
        topics = []
        for params in ([{"per_page": "100"}] +
                       ([{"per_page": "100", "only_announcements": "true"}] if include_announcements else [])):
            url = f"{self.base_url}/api/v1/courses/{course_id}/discussion_topics"
            try:
                topics.extend(await self._get_paged(url, params))
            except CanvasApiError as exc:
                logger.debug("discussion_topics listing failed: %s", exc)
        return topics

    async def list_discussion_entries(self, course_id: str, topic_id: str) -> list:
        url = f"{self.base_url}/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries"
        return await self._get_paged(url, {"per_page": "100"})

    async def list_assignments(self, course_id: str) -> list:
        url = f"{self.base_url}/api/v1/courses/{course_id}/assignments"
        return await self._get_paged(url, {"per_page": "100"})

    async def list_quizzes(self, course_id: str) -> list:
        url = f"{self.base_url}/api/v1/courses/{course_id}/quizzes"
        return await self._get_paged(url, {"per_page": "100"})

    async def get_course_syllabus(self, course_id: str) -> dict:
        url = f"{self.base_url}/api/v1/courses/{course_id}"

        async def _do(headers: dict[str, str]) -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.get(
                    url, headers=headers, params={"include[]": "syllabus_body"},
                )

        resp = await self._request_with_401_retry(_do, "GET course syllabus")
        self._raise_for_status(resp, "GET course syllabus")
        return resp.json()

    # ---- Internals ----------------------------------------------------

    async def _get_paged(self, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Walk Canvas's Link-header pagination, returning all rows."""

        async def _fetch_one(
            client: httpx.AsyncClient, u: str, p: dict[str, str] | None
        ) -> httpx.Response:
            async def _go() -> httpx.Response:
                async def _req(headers: dict[str, str]) -> httpx.Response:
                    return await client.get(u, headers=headers, params=p)
                resp = await self._request_with_401_retry(_req, "list (paged)")
                self._raise_for_status(resp, "list (paged)")
                return resp

            return await retry_with_backoff(
                _go,
                max_attempts=_RETRY_MAX_ATTEMPTS,
                base_delay=_RETRY_BASE_DELAY,
                max_delay=_RETRY_MAX_DELAY,
                operation_name="canvas._get_paged",
            )

        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            next_url: str | None = url
            next_params: dict[str, str] | None = params
            while next_url:
                resp = await _fetch_one(client, next_url, next_params)
                results.extend(resp.json())
                next_url = _next_link(resp.headers.get("Link"))
                next_params = None
        return results

    def _raise_for_status(self, resp: httpx.Response, op: str) -> None:
        if resp.is_success:
            return
        logger.debug("Canvas %s -> %d: %s", op, resp.status_code, resp.text[:500])
        raise CanvasApiError(resp.status_code, f"{op} failed")


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip()
        if seg.endswith('rel="next"'):
            return seg.split(";", 1)[0].strip().strip("<>")
    return None


def _resolve_default_token() -> str:
    token = getattr(settings, "canvas_api_token", None)
    if not token:
        raise ValueError("CanvasClient: settings.canvas_api_token is required")
    if hasattr(token, "get_secret_value"):
        token = token.get_secret_value()
    return str(token)
