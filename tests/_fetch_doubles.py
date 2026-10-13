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
    """The route spec as a real ``HttpFetchResult``.

    ``HttpFetchResult`` rather than nothing: every branch that returns one here
    does, and the branch that does not raise. Left unannotated the answer was
    ``Any``, which is what let a fetcher double's return type be ``Any`` too --
    a chain in which the one place that actually builds the result said nothing
    about it.
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
    """The production fetcher contract, the request seam, and the observations.

    This was a plain function with ``calls`` attached to it after construction,
    so the observations were invisible to every type that saw the return: a test
    reading ``fetcher.calls`` was reaching for something only the factory could
    promise. The class is where that promise lives, and it is still a real
    ``HttpFetcher`` everywhere production asks for one -- ``app.orchestration.worker``
    recognises a callable object by its ``__call__``, so the async-discipline
    check sees exactly what it saw before.

    ``answer`` takes the whole request and returns a :data:`Route` unless
    ``answer_is_response`` says otherwise; signature and cap are production's.
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
        """What the strategy reads, whichever way this factory was built.

        ``HttpFetchResult`` is the contract production consumes, and both arms
        below satisfy it: ``_answer`` builds one from a route spec, and the
        ``answer_is_response`` arm hands back the caller's own object, which is
        shaped like one. Declaring it here rather than ``Any`` is what lets a
        caller of a fetcher double know what it got.

        The answer callback receives the admitted ``NormalizedUrl`` as its
        first argument -- this is the one place that contract lives, so every
        factory's ``respond`` reads ``url.path`` / ``url.site`` / ``url.raw``
        and never ``urlparse(url)``; a respond written against text is
        responding to a request production never made.
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


def site_fetcher(pages: dict[str, Any]):
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
    return {r.normalized_url.raw for r in out["results"]}


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
    """A fetcher that serves one fixed response for whatever URL it is handed.

    Most fetcher doubles in this tree answer every URL the same way, and
    spelling that out meant re-stating the production fetcher's signature at
    each site. That is the copy the ``scripted-fetcher`` funnel bans: the
    signature, and the cap's default, are owned by :func:`fetcher_double`, so
    a change to the real fetcher lands here once.

    ``headers`` replaces the response headers wholesale (``content-type``
    included), which is what a test about a header or a content-encoding needs;
    otherwise ``ctype`` supplies the content type and nothing else is sent.
    """
    return FetcherDouble(lambda url, *_: {"status": status, "body": body, "headers": headers, "ctype": ctype})


def failing_fetcher(error: BaseException | type[BaseException] | None = None, calls: list[str] | None = None):
    """A fetcher that raises instead of responding, optionally recording URLs.

    ``calls`` turns this into a recorder, so a test that asserts the engine
    retried the same URL three times does not need its own closure for it.

    A class is instantiated per call, so each raise is a distinct exception
    with its own traceback; pass an instance only when the exception carries
    state a test needs to inspect (``httpx.HTTPStatusError`` and its response).
    """
    return FetcherDouble(lambda url, *_: error if error is not None else ConnectionError, calls)
