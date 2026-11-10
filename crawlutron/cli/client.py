"""HTTP client for the crawl service: a thin transport facade.

No SSRF, policy, or URL validation happens here; the service is the single
source of truth and decides what is crawlable.
"""

from __future__ import annotations

import httpx

__all__ = ["CrawlClient", "CrawlClientError", "TransportError"]


class CrawlClientError(Exception):
    """Raised when the CLI cannot complete a request to the service."""


class TransportError(CrawlClientError):
    """Network-level failure: refused connection, DNS, or timeout."""


class CrawlClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        if client is not None:
            self._client = client
            if api_key:
                client.headers["X-API-Key"] = api_key
        else:
            headers: dict[str, str] = {}
            if api_key:
                headers["X-API-Key"] = api_key
            self._client = httpx.Client(base_url=self._base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> CrawlClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def crawl(self, payload: dict[str, object]) -> httpx.Response:
        """POST one crawl request to the service (sync or async per payload)."""
        try:
            return self._client.post(f"{self._base_url}/v1/crawl", json=payload)
        except httpx.TransportError as exc:
            raise TransportError(f"cannot reach crawl service at {self._base_url}: {exc}") from exc

    def result(self, request_id: str) -> httpx.Response:
        """Poll the stored result for ``request_id``."""
        try:
            return self._client.get(f"{self._base_url}/v1/crawl/{request_id}")
        except httpx.TransportError as exc:
            raise TransportError(f"cannot reach crawl service at {self._base_url}: {exc}") from exc

    def trace(self, request_id: str) -> httpx.Response:
        """Fetch the strategy trace for ``request_id``."""
        try:
            return self._client.get(f"{self._base_url}/v1/crawl/{request_id}/trace")
        except httpx.TransportError as exc:
            raise TransportError(f"cannot reach crawl service at {self._base_url}: {exc}") from exc

    def health(self) -> httpx.Response:
        """GET /health liveness check."""
        try:
            return self._client.get(f"{self._base_url}/health")
        except httpx.TransportError as exc:
            raise TransportError(f"cannot reach crawl service at {self._base_url}: {exc}") from exc
