"""Scripted fetchers: hanging, failing, routed and concurrent."""

import asyncio

from app.models.normalized_url import NormalizedUrl
from tests._fetch_doubles import FetcherDouble, Route


def hanging_fetcher(calls: list[str] | None = None):
    """A fetcher that never returns, so only the caller's deadline ends it.

    The tarpit, for a poisoned URL whose fetch has to time out. It awaits a
    never-resolved future rather than sleeping: the suite monkeypatches
    ``asyncio.sleep`` to return instantly, which would turn the tarpit into a
    fast success and assert nothing.
    """

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

    Almost every hand-rolled fetcher in this tree is the same shape with the
    same three branches -- robots, sitemap, page -- and the branches are what
    makes them worth stating once. ``routes`` maps a URL substring to what that
    fetch should do; the first match wins, so order the specific before the
    general. A route may be an exception (raised), a ``(status, body, ctype)``
    triple, a :func:`make_fetch` kwargs dict, or a prebuilt ``HttpFetchResult``.

    ``calls`` records every URL fetched, which is the assertion most of these
    tests exist to make. ``on_route(url, headers)`` is called when a route
    matches, for a test that counts one *kind* of fetch across fetcher instances
    it does not want to share a fetcher between -- the sharing under test is
    elsewhere, and building a second fetcher per request is the honest way to
    show it.

    ``missing`` is what happens when nothing matches: ``"default"`` serves
    ``default``; ``"raise"`` raises an ``AssertionError`` naming the URL, for a
    test that must not be fetching something it did not script. The default is
    the 404, matching :func:`site_fetcher`, because a crawler test that reads a
    body it did not arrange is a test that passes for the wrong reason.
    """

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

    The shape behind "the first strategy gets a CAPTCHA and the escalated one
    gets the page", or "the first attempt drops and the retry recovers". Writing
    it as a closure means an ``if calls == 1`` branch per test and no statement
    of what happens on the third call, which is where these go wrong.

    ``repeat_last`` serves the final step for every call past the list, so a
    test that only cares about the transition still terminates.
    """
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

    The conditional-request seam: what a sitemap or page fetch returns depends
    on the ``If-None-Match`` / ``If-Modified-Since`` header the crawler sent, so
    the branch cannot be keyed on the URL. ``respond(url, headers)`` -- the
    request, not the whole contract -- returns whatever :func:`routed_fetcher`'s
    routes may, or a finished response when ``answer_is_response`` is set.

    Distinct from :func:`tap_fetcher` (which delegates to a real fetcher) and
    from :func:`concurrent_fetcher` (which also counts overlap): this one adds
    no scheduling point, so a test about headers does not acquire a
    concurrency behaviour it did not ask for. ``fetcher.headers`` is the header
    log, which is what this seam is for -- and it is its own member rather than
    ``calls`` so that "which URLs" and "what was sent" stay two different facts.
    """
    seen: list[dict[str, str]] = [] if record_headers is None else record_headers
    return FetcherDouble(lambda url, sent_headers, *_: respond(url, sent_headers), record_headers=seen)


def tap_fetcher(inner, calls: list[str] | None = None):
    """Wrap another fetcher, recording the URLs that reach it.

    For a test whose subject is a *real* fetcher (the default HTTP fetcher, a
    site fetcher) and whose only extra requirement is to see what was asked
    for. Recording and delegating are separate concerns, so they are separate
    functions: this one owns the list, the wrapped one owns the response --
    which may be sync or async, since :func:`fetcher_double` awaits either.
    """
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

    ``respond(url, headers)`` maps a request to a route spec (see
    :func:`routed_fetcher`) and may be a plain callable or a coroutine function.
    It takes the request for the same reason :func:`request_fetcher` does -- one
    ``respond`` shape across the seam -- and because a fetcher that is deciding
    anything usually needs the headers as much as the URL.

    The counter is raised on entry and lowered on exit, with ``yield_times`` loop
    yields in between: the yield is the scheduling point that makes overlap
    observable at all, and without it an async fetcher that never awaits
    serialises itself and any concurrency assertion passes for free.

    ``peak`` on the returned fetcher is what a concurrency test should assert.
    """
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
