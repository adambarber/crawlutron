"""The one import door for every fetch double (#314, 3.3).

One construction per concern: ``FetcherDouble`` is the primitive, ``make_fetch``
builds the result it answers with, and ``Route``/``PageData``/``PageMap`` are the
input grammar a test expresses its answer in. Callers write the small answer
closure at the site when the response depends on request or state."""

import inspect
from collections.abc import Callable
from typing import Any

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from tests._policy_factories import canonical

#: One scripted page: (status, raw body, content type). The fetcher estimates
#: the type when a page does not declare one; declaring it makes a sitemap.
PageData = tuple[int, str, str]
PageMap = dict[str, PageData]


def make_fetch(
    status=200,
    body=b"<html><body>hello</body></html>",
    ctype="text/html; charset=utf-8",
    final_url="http://example.com/",
    url="http://example.com/",
    headers=None,
    decompression_failed=False,
):
    return HttpFetchResult(
        url=canonical(url),
        final_url=canonical(final_url),
        status_code=status,
        headers={"content-type": ctype} if headers is None else headers,
        body=body,
        elapsed_ms=1,
        decompression_failed=decompression_failed,
    )


#: What a route may answer with.
Route = Any


def _answer(spec: Route, url: NormalizedUrl) -> HttpFetchResult:
    """The route spec as a real ``HttpFetchResult``; every branch that returns one
    here does, and the branch that does not raises. Declared, it stops a fetcher
    double's return type drifting back to ``Any`` at the one place it is built.
    """
    if isinstance(spec, BaseException) or (isinstance(spec, type) and issubclass(spec, BaseException)):
        raise spec() if isinstance(spec, type) else spec
    if isinstance(spec, HttpFetchResult):
        return spec
    if isinstance(spec, dict):
        return make_fetch(url=url, final_url=url, **spec)
    status, body, ctype = spec
    if isinstance(body, str):
        body = body.encode("utf-8")
    return make_fetch(url=url, final_url=url, status=status, body=body, ctype=ctype)


class FetcherDouble:
    """The production fetcher contract, the request seam, and its observations; a
    plain function with ``calls`` attached made them invisible to every type that
    saw it, and it stays callable so production's HttpFetcher checks still apply.
    """

    calls: list[str]
    peak: Callable[[], int]
    inflight: Callable[[], int]
    headers: list[dict[str, str]]

    def __init__(
        self,
        answer: Callable[..., Route],
        calls: list[str] | None = None,
        *,
        record_headers: list[dict[str, str]] | None = None,
        record_cap: list[int] | None = None,
        answer_is_response: bool = False,
    ) -> None:
        self._answer = answer
        self._record_cap = record_cap
        self._answer_is_response = answer_is_response
        self.calls = [] if calls is None else calls
        self.headers = [] if record_headers is None else record_headers

    async def __call__(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        timeout_seconds: int,
        max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    ) -> HttpFetchResult:
        """What the strategy reads, whichever way this factory was built: declared as
        ``HttpFetchResult`` so a caller knows what it got, not ``Any``. The answer
        receives the admitted ``NormalizedUrl`` -- the one place that contract lives.
        """
        # ``calls`` speaks text: it is what tests assert membership in, and the wire form of the value is the
        # text the request was made to.
        self.calls.append(url.raw)
        self.headers.append(dict(headers))
        if self._record_cap is not None:
            self._record_cap.append(max_content_length)
        result = self._answer(url, headers, timeout_seconds, max_content_length)
        if inspect.isawaitable(result):
            result = await result
        # The flag says which of the two answer shapes this factory produces, and each arm now says so out
        # loud.
        if self._answer_is_response:
            # The answer already IS the fetch result, so it goes back untouched. A round trip through
            # ``_answer`` would be a second reading of it.
            return result
        return _answer(result, url)
