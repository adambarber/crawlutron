"""The ONLY declaration of the fetcher call shape, a Protocol: four positional params
``(url, headers, timeout_seconds, max_content_length)``, the fourth optional -- which
a ``Callable`` alias cannot express -- plus the keyword ``policy``, ``impersonate``, ``pin``.
"""

from __future__ import annotations

from typing import Protocol

from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl


class HttpFetcher(Protocol):
    """A bound fetcher's four positional parameters; also the pre-bound shape --
    a production fetcher is this call plus required keyword-only ``policy`` and
    optional ``impersonate``/``pin`` (``None`` = the transport's real rule, safe to omit).
    """

    async def __call__(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        timeout_seconds: int,
        max_content_length: int = ...,
    ) -> HttpFetchResult: ...
