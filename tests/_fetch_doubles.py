"""Core fetch doubles: results, pages and scripted sites."""

import inspect
from collections.abc import Callable
from typing import Any

from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl
from app.orchestration.engine import crawl_request
from app.transport.cache import HttpCache
from app.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from tests._policy_factories import canonical

SCOPE_KEEP_DROP_PAGES = {
    "/": (
        200,
        '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>',
        "text/html",
    ),
    "/keep/x": (200, "<html>k</html>", "text/html"),
    "/drop/y": (200, "<html>d</html>", "text/html"),
}

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
        sent_headers: dict[str, str],
        timeout: int,
        max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    ) -> HttpFetchResult:
        """What the strategy reads, whichever way this factory was built: declared as
        ``HttpFetchResult`` so a caller knows what it got, not ``Any``. The answer
        receives the admitted ``NormalizedUrl`` -- the one place that contract lives.
        """
        # ``calls`` speaks text: it is what tests assert membership in, and the wire form of the value is the
        # text the request was made to.
        self.calls.append(url.raw)
        self.headers.append(dict(sent_headers))
        if self._record_cap is not None:
            self._record_cap.append(max_content_length)
        result = self._answer(url, sent_headers, timeout, max_content_length)
        if inspect.isawaitable(result):
            result = await result
        # The flag says which of the two answer shapes this factory produces, and each arm now says so out
        # loud.
        if self._answer_is_response:
            # The answer already IS the fetch result, so it goes back untouched. A round trip through
            # ``_answer`` would be a second reading of it.
            return result
        return _answer(result, url)


def site_fetcher(pages: PageMap):
    """A fetcher serving a scripted page table by path, 404 for anything else.

    A crawler test that reads a body it did not arrange passes for the wrong
    reason, so an unscripted path is a 404 rather than a default page.
    """

    def answer(url: NormalizedUrl, *_):
        entry = pages.get(url.path or "/")
        if entry is None:
            return {"status": 404, "body": b"<html></html>", "ctype": "text/html"}
        status, body, ctype = entry
        return {
            "status": status,
            "body": body.encode("utf-8") if isinstance(body, str) else body,
            "ctype": ctype,
        }

    return FetcherDouble(answer)


async def run_scoped_crawl(scope_regex=None, *, max_depth=1, max_pages=10, pages=None):
    pages = pages if pages is not None else SCOPE_KEEP_DROP_PAGES
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": max_depth, "max_pages": max_pages}, "scope_regex": scope_regex},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    return {r.normalized_url.raw for r in out["results"] if r.normalized_url is not None}


def ok_fetcher(url, sent_headers, timeout, max_content_length=DEFAULT_MAX_CONTENT_LENGTH_BYTES):
    """The 200-for-everything fetcher, passed around as a value.

    Callers hand this function itself to the engine, so it is a fetcher rather
    than a factory; the cap still comes from production's constant.
    """
    return make_fetch(url=url)


def raise_fetcher(url, sent_headers, timeout, max_content_length=DEFAULT_MAX_CONTENT_LENGTH_BYTES):
    """The network-is-down fetcher, passed around as a value like ``ok_fetcher``."""
    raise ConnectionError("network down")


def scripted_fetcher(*, status=200, body=b"", headers=None, ctype="text/html; charset=utf-8"):
    """A fetcher that serves one fixed response for every URL; the
    ``scripted-fetcher`` funnel bans re-stating the production signature, which is
    owned by :func:`fetcher_double`, and ``headers`` replaces the response wholesale.
    """
    return FetcherDouble(lambda url, *_: {"status": status, "body": body, "headers": headers, "ctype": ctype})


def failing_fetcher(error: BaseException | type[BaseException] | None = None, calls: list[str] | None = None):
    """A fetcher that raises instead of responding, optionally recording URLs; with
    ``calls`` it becomes a recorder, and a class is raised per call so each failure
    is distinct -- pass an instance only when the exception must carry state.
    """
    return FetcherDouble(lambda url, *_: error if error is not None else ConnectionError, calls)
