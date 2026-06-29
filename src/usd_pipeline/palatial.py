"""HTTP client for the Palatial dashboard API."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_RETRYABLE_NET = (httpx.TimeoutException, httpx.TransportError)
_RETRYABLE_STATUS = (408, 429)


def _redact_url(url: str) -> str:
    """Drop query string (presigned signatures) so errors/logs never leak credentials."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _backoff(attempt: int) -> float:
    return min(10.0, 0.5 * 2 ** (attempt - 1))


class PalatialError(Exception):
    pass


class PalatialHTTPError(PalatialError):
    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}".strip())


class _Retryable(PalatialError):
    """Internal: signals a transient failure that tenacity should retry."""


class PalatialClient:
    def __init__(
        self,
        *,
        base_url: str = "https://dashboard.palatial.cloud/api/v1",
        cookie: str = "",
        timeout: float = 60.0,
        max_retries: int = 4,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self._max_retries = max(1, max_retries)
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._send = self._build_send()

    def __enter__(self) -> PalatialClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _build_send(self):
        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=10),
            retry=retry_if_exception_type(_Retryable),
            reraise=True,
        )
        def send(method: str, url: str, **kwargs) -> httpx.Response:
            try:
                resp = self._client.request(method, url, **kwargs)
            except _RETRYABLE_NET as e:  # network/timeout
                raise _Retryable(str(e)) from e
            if resp.status_code >= 500 or resp.status_code in _RETRYABLE_STATUS:
                raise _Retryable(f"retryable status {resp.status_code}")
            return resp

        return send

    # --- endpoints -------------------------------------------------------

    def search(self, q: str = "", page: int = 1, limit: int = 812) -> dict:
        resp = self._send(
            "GET",
            f"{self.base_url}/library/search",
            params={"q": q, "page": page, "limit": limit},
        )
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        return resp.json()

    def mint_viewer_cookie(self, asset_id: str) -> str:
        resp = self._send(
            "POST",
            f"{self.base_url}/share/library-viewer-session",
            json={"assetId": asset_id},
        )
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        token = resp.cookies.get("asset_share_viewer")
        if not token:
            raise PalatialError("no asset_share_viewer cookie in response")
        return token

    def asset_detail(self, asset_id: str, cookie: str | None = None) -> dict:
        if self.cookie:
            cookie_header = self.cookie
        elif cookie:
            cookie_header = f"asset_share_viewer={cookie}"
        else:
            cookie_header = None
        headers = {"Cookie": cookie_header} if cookie_header else {}
        resp = self._send("GET", f"{self.base_url}/assets/{asset_id}", headers=headers)
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        return resp.json()

    def process_file(self, asset_id: str, kind: str) -> dict:
        resp = self._send("GET", f"{self.base_url}/assets/{asset_id}/process-file/{kind}")
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        return resp.json()

    def embedded_physics(self, asset_id: str) -> dict:
        resp = self._send("GET", f"{self.base_url}/assets/{asset_id}/embedded/physics")
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        return resp.json()

    def media_validation_report(self, asset_id: str) -> dict:
        resp = self._send("GET", f"{self.base_url}/assets/{asset_id}/media/validation-report")
        if resp.status_code >= 400:
            raise PalatialHTTPError(resp.status_code, resp.text[:200])
        return resp.json()

    @contextlib.contextmanager
    def download(self, url: str) -> Iterator[httpx.Response]:
        """Yield a streaming response for an absolute (presigned) URL."""
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._client.stream("GET", url) as resp:
                    if resp.status_code >= 500 and attempt < self._max_retries:
                        time.sleep(_backoff(attempt))
                        continue
                    if resp.status_code >= 400:
                        raise PalatialHTTPError(resp.status_code, _redact_url(url))
                    yield resp
                    return
            except _RETRYABLE_NET as e:
                if attempt >= self._max_retries:
                    raise PalatialError(f"download failed: {e}") from e
                time.sleep(_backoff(attempt))
