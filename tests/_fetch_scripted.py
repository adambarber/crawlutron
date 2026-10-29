"""Scripted fetchers: hanging, failing, routed and concurrent."""

import asyncio

from crawlutron.models.normalized_url import NormalizedUrl
from tests._fetch_doubles import FetcherDouble, Route


def hanging_fetcher(calls: list[str] | None = None):
    """A fetcher that never returns, so only the caller's deadline ends it.

    Awaits a never-resolved future rather than sleeping: the suite patches
    ``asyncio.sleep``, which would make the tarpit a fast success."""

    async def never(url, *_):
        await asyncio.Future()

    return FetcherDouble(never, calls)


def routed_fetcher(
    routes: dict[str, Route] | None = None,
    calls: list[str] | None = None,
    *,
    default: Route = (404, b"<html></html>", "text/html"),
    missing="default",
    on_route=None,
):
    """A fetcher that answers per-URL, by substring match on the URL.

    ``routes`` maps a substring to an exception, a ``(status, body, ctype)``
    triple, kwargs, or a prebuilt result (first match wins); ``missing`` is 404."""

    def answer(url: NormalizedUrl, sent_headers, *_):
        for fragment, spec in (routes or {}).items():
            if fragment in url.raw:
                if on_route is not None:
                    on_route(url, sent_headers)
                return spec
        if missing == "raise":
            raise AssertionError(f"unscripted fetch: {url} (routes: {sorted(routes or {})})")
        return default

    return FetcherDouble(answer, calls)


def sequenced_fetcher(
    steps: list[Route],
    calls: list[str] | None = None,
    *,
    repeat_last: bool = True,
):
    """A fetcher that answers in call order, for a test about what happens next.

    The shape behind "CAPTCHA then the escalated page" or "drop then retry";
    ``repeat_last`` serves the final step past the list."""
    if not steps:
        raise ValueError("sequenced_fetcher needs at least one step")
    state = {"n": 0}

    def answer(url, *_):
        # The harness owns the position, not the caller's list: a test that never asks for the call log still
        # gets a sequence that advances.
        index = state["n"]
        state["n"] += 1
        if index >= len(steps):
            if not repeat_last:
                raise AssertionError(f"unscripted fetch #{index + 1}: {url}")
            index = len(steps) - 1
        return steps[index]

    fetcher = FetcherDouble(answer, calls)
    return fetcher


def request_fetcher(respond, record_headers: list[dict[str, str]] | None = None):
    """A fetcher whose answer is computed from ``(url, headers)``.

    The conditional-request seam: the answer depends on ``If-None-Match`` /
    ``If-Modified-Since``, so the branch keys on the request."""
    seen: list[dict[str, str]] = [] if record_headers is None else record_headers
    return FetcherDouble(lambda url, sent_headers, *_: respond(url, sent_headers), record_headers=seen)


def tap_fetcher(inner, calls: list[str] | None = None):
    """Wrap another fetcher, recording the URLs that reach it.

    For a test whose subject is a REAL fetcher and whose only extra need is to
    see what was asked for; this owns the list, the wrapped one the response."""
    return FetcherDouble(
        lambda url, sent_headers, timeout, cap: inner(url, sent_headers, timeout, cap), calls
    )


def concurrent_fetcher(
    respond,
    calls: list[str] | None = None,
    *,
    yield_times: int = 1,
):
    """A fetcher that tracks how many of its calls are in flight at once.

    ``respond(url, headers)`` maps to a route spec; the counter rises on entry
    and falls on exit with ``yield_times`` yields between. ``peak`` is to assert."""
    state = {"inflight": 0, "peak": 0}

    async def answer(url, sent_headers, *_args):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            # ``_yield``, not ``_``: ``*_args`` above already binds the rest of the arguments, and a bare
            # ``_`` loop variable is a second name for a value the enclosing call still needs.
            for _yield in range(yield_times):
                await asyncio.sleep(0)
            return respond(url, sent_headers)
        finally:
            state["inflight"] -= 1

    fetcher = FetcherDouble(answer, calls)
    fetcher.peak = lambda: state["peak"]
    fetcher.inflight = lambda: state["inflight"]
    return fetcher
