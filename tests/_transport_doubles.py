"""Transport-level test seams: a scripted httpx transport, and the isolated
interpreter the genuine curl_cffi fetcher has to run in.

The httpx path is REPLACED rather than imitated. The client below is a real
``httpx.AsyncClient`` over ``httpx.MockTransport`` and the responses are real
``httpx.Response`` objects carrying a real async body stream, so the fetcher's
own call -- ``async with client.stream(...) as response: async for chunk in
response.aiter_raw()`` -- is the production call, with the production timeout
object and the production redirect chain in front of it. The stand-in this
replaced had a plain ``dict`` for headers (so no case-insensitive lookup and no
duplicate header), and a ``stream()`` that returned the response instead of an
async context manager, so its ``__aenter__`` was a fiction the real client
never needs. None of that could fail when the fetcher changed how it reads a
response, which is the only thing a transport double is for.

The cffi path cannot be replaced at all: in-process real curl_cffi busy-spins
the GIL under the suite's zero-second sleep patch and wedges the whole pytest
process, so the genuine fetcher runs in a spawned interpreter with a hard join
(see the ``real_cffi`` marker and ``conftest.stub_cffi_fetcher``).
:func:`real_cffi_fetch_outcome` owns that arrangement -- the child, the queue,
the join, the terminate -- and a test states only the outcome it is about: a
status code, or the class of the transport failure that ended the fetch.

"Spawned" is a decision this module makes, not a platform default it inherits.
The default start method is ``fork`` on Linux and ``spawn`` on macOS, and a fork
child is a copy of the test process: it inherits the ``monkeypatch``ed
``impersonating_fetcher`` stub and the zeroed ``time.sleep``. On a fork platform
the isolation this file exists to provide silently did not happen, so a test
marked ``real_cffi`` got the stub, and curl's LOW_SPEED watchdog -- which depends
on real sleeps -- busy-spun in the child until the hang guard killed it. Both
symptoms were platform-local: on macOS the tests passed. :func:`isolated_process`
and :func:`isolated_queue` are the one place the start method is named, so the
isolation is a property of the harness rather than of the kernel it runs on.
"""

import multiprocessing
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES

_ISOLATED = multiprocessing.get_context("spawn")


def isolated_queue() -> Any:
    """A queue from the same context as :func:`isolated_process`.

    The two must agree: a queue created by the default context and handed to a
    spawned process is the kind of pairing that works until it does not.
    """
    return _ISOLATED.Queue()


def isolated_process(target: Any, args: tuple[Any, ...]) -> multiprocessing.Process:
    """A child process that inherits nothing from this test process.

    Named rather than inlined because the start method is the whole point: a
    default ``multiprocessing.Process`` is isolated on macOS and a fork on Linux,
    so a test written against it is a test that passes on the author's machine.
    """
    return _ISOLATED.Process(target=target, args=args)


class _ScriptedBody(httpx.AsyncByteStream):
    """The bytes a scripted server sent, delivered as a stream.

    ``httpx.Response(content=...)`` reads the body eagerly and marks it consumed,
    so the fetcher's ``aiter_raw()`` would raise ``StreamConsumed`` against it.
    A response that has not been read yet is what a transport hands back, and
    building one from the public ``AsyncByteStream`` base is how a test says so.
    """

    def __init__(self, body: bytes) -> None:
        self._chunks = [body] if body else []

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class MockResponse(httpx.Response):
    """A real ``httpx.Response`` with a positional-friendly constructor.

    The name stays because it is the suite's name for "a response a scripted
    transport returns"; what changed is what it is. Headers are
    ``httpx.Headers`` again -- case-insensitive, and able to hold two
    ``Set-Cookie`` lines where a dict could hold one -- and the body is a
    stream, so ``aiter_raw()`` reads it the way the fetcher reads it.
    """

    def __init__(self, status_code: int, headers: dict[str, str] | None = None, content: bytes = b"") -> None:
        super().__init__(status_code, headers=headers, stream=_ScriptedBody(content))


def _recording_hook(sink: list):
    """An httpx event hook that appends every event to `sink`.

    A coroutine function because httpx *awaits* the result; a hook that returns
    ``None`` fails at the client, not at the test that installed it.
    """

    async def hook(event):
        sink.append(event)

    return hook


class ScriptedClient(httpx.AsyncClient):
    """An ``httpx.AsyncClient`` that also keeps the headers it was called with.

    A real client, because everything around the answer is what httpx actually
    does: the request the fetcher built, the timeout it passed, the streaming
    read, the close on the way out. ``captured_headers`` used to be poked onto
    the instance after construction, which put a member on the object that its
    own type did not declare -- so a test reading it was reaching for something
    only the factory could promise. The type is where that promise belongs.
    """

    def __init__(self, *args, captured_headers: list[httpx.Headers], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.captured_headers = captured_headers


def scripted_transport_client(
    responses: Sequence[httpx.Response],
    *,
    observed_requests: list[httpx.Request] | None = None,
    observed_responses: list[httpx.Response] | None = None,
) -> httpx.AsyncClient:
    """A real ``httpx.AsyncClient`` that answers from `responses`, in call order.

    ``httpx.MockTransport`` is the seam httpx itself publishes for this, so
    everything around the answer is production: the request the fetcher built,
    the timeout it passed, the streaming read, the close on the way out. A
    scripted response beyond the list raises ``IndexError`` rather than
    repeating an answer, because a fetch that asked for more hops than the
    script has is a fact about the test, not a reason to invent one.

    ``captured_headers`` holds one real ``httpx.Headers`` per request, in order.

    ``observed_requests`` / ``observed_responses`` collect the client's own event
    hooks, which is the only way to see the full ``httpx.Request`` (the URL the
    fetcher caused, not just its headers) or the full ``httpx.Response`` -- and
    therefore whether a redirect was followed by the fetcher's own loop or by
    httpx, which is the difference ``response.history`` records and headers
    cannot.
    """
    captured: list[httpx.Headers] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return responses[len(captured) - 1]

    client = ScriptedClient(transport=httpx.MockTransport(_handle), captured_headers=captured)
    if observed_requests is not None:
        client.event_hooks["request"].append(_recording_hook(observed_requests))
    if observed_responses is not None:
        client.event_hooks["response"].append(_recording_hook(observed_responses))
    return client


def dispatching_transport_client(handler) -> httpx.AsyncClient:
    """A real ``httpx.AsyncClient`` whose transport asks ``handler`` per request.

    :func:`scripted_transport_client` answers from a fixed list, which is the right
    shape when a test knows its hops up front and the wrong one when the answer
    depends on where the request went -- a redirect chain, where each ``Location``
    is only known once the previous hop answered. Everything else is the same: a
    real client, a real ``httpx.Request``, real ``httpx.Response`` objects with a
    real streaming body. Only who decides the answer differs.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def allow_scripted_egress(monkeypatch, ip: str = "1.2.3.4") -> None:
    """Stand the SSRF guard down for a target no resolver could answer for.

    Two things stand between a fetch and a socket and they are separate
    concerns: what *answers* the request (:func:`scripted_transport_client`) and
    what the SSRF guard *sees*. A script has no DNS, so the two production
    seams that ask the network those questions are the two that must stand
    down. Everything downstream of them still runs -- the redirect chain's
    host bookkeeping, the header stripping on a cross-host hop, the declared
    length -- so a test using this is still a test of the fetcher.

    ``ip`` is what the pinned host resolves to. It is a public address by
    construction, because a pinned private address is exactly what the guard
    would have rejected and a test here is not about the guard.
    """
    from app.crawl.strategies import pinned_transport as pinned_mod

    monkeypatch.setattr(pinned_mod, "check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(pinned_mod, "resolve_and_pin_host", lambda url: (urlparse(url).hostname, ip))


def patch_http_fetcher_transport(
    monkeypatch,
    responses: Sequence[httpx.Response],
    *,
    allow_ssrf_bypass: bool,
) -> httpx.AsyncClient:
    """Point the httpx fetcher at a scripted transport; return the client.

    ``allow_ssrf_bypass`` is required rather than defaulted because installing
    this also installs :func:`allow_scripted_egress`, and a test that reaches
    that by accident is a test whose SSRF verdict means nothing.
    """
    if not allow_ssrf_bypass:
        raise ValueError("patch_http_fetcher_transport requires allow_ssrf_bypass=True")

    from app.crawl.strategies import http_client_pool as pool_mod

    client = scripted_transport_client(responses)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)
    allow_scripted_egress(monkeypatch)
    return client


class CffiFetchOutcome:
    """What the genuine fetcher did in its own interpreter.

    Either a status code (``ok``) or the class name of the transport failure
    that ended the fetch. The two are one value rather than a tuple with a
    "which slot is real" convention, so a test cannot read the wrong one.
    """

    __slots__ = ("detail", "error", "ok", "status")

    def __init__(
        self, *, ok: bool, status: int | None = None, error: str | None = None, detail: str = ""
    ) -> None:
        self.ok = ok
        self.status = status
        self.error = error
        self.detail = detail

    def __repr__(self) -> str:
        if self.ok:
            return f"CffiFetchOutcome(ok, status={self.status})"
        return f"CffiFetchOutcome(err, error={self.error!r}, detail={self.detail!r})"


def _genuine_cffi_outcome(url: str, timeout_seconds: int, max_content_length: int) -> CffiFetchOutcome:
    """Fetch `url` with the real fetcher, right here, and classify the result.

    A transport failure is contract-fine and a hang is not: any exception the
    real curl_cffi raises ends the fetch in bounded time, so the class name is
    recorded rather than re-raised and the caller decides what it meant.
    """
    import asyncio

    from app.crawl.strategies.fetchers import impersonating_fetcher
    from app.validation import HostPolicy

    async def _fetch():
        return await impersonating_fetcher(HostPolicy.unrestricted())(
            url,
            {},
            timeout_seconds,
            max_content_length,
        )

    try:
        fetched = asyncio.run(_fetch())
    except Exception as failure:
        return CffiFetchOutcome(ok=False, error=type(failure).__name__, detail=str(failure)[:120])
    return CffiFetchOutcome(ok=True, status=fetched.status_code)


def _outcome_worker(url: str, timeout_seconds: int, max_content_length: int, out_queue: Any) -> None:
    """Subprocess body: hand the parent one outcome and exit.

    Module-level because a spawn target has to be importable in the child, and
    separate from :func:`_genuine_cffi_outcome` so that classification -- which
    the parent asserts on -- is decided in the same interpreter as the fetch.
    """
    out_queue.put(_genuine_cffi_outcome(url, timeout_seconds, max_content_length))


def join_or_kill(worker, join_timeout: float, what: str) -> None:
    """Wait ``join_timeout`` for ``worker``; kill it and fail if it is still running.

    The guard every spawned-interpreter test needs, and the part that goes wrong
    quietly: a bare ``join(timeout)`` returns, the test proceeds, and the wedged
    child keeps running -- a curl watchdog busy-spinning the GIL is exactly the
    process that then takes the whole suite down with it. Written per test, the
    terminate step is the part that gets dropped.

    ``what`` names the work in the failure message, because "a worker hung" in a
    2000-test run is not enough to find the test.
    """
    worker.join(join_timeout)
    if worker.is_alive():
        worker.terminate()
        worker.join()
        pytest.fail(f"{what} was still running {join_timeout}s after the join")


def real_cffi_fetch_outcome(
    url: str,
    *,
    timeout_seconds: int = 10,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    join_timeout: float = 10.0,
) -> CffiFetchOutcome:
    """Run the genuine curl_cffi fetcher against `url` in a spawned interpreter.

    The child inherits this process's environment, so a loopback target needs
    ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1`` set here (and undone by
    monkeypatch afterwards) rather than inside the child.

    ``join_timeout`` is a hang guard, not a budget: the fetch's own
    ``timeout_seconds`` is the budget, and a run that outlasts the guard is
    reported as the hang it is instead of being waited out. The bound is a
    parameter because it is not a production constant and must not be
    re-spelled per test.
    """
    out_queue = isolated_queue()
    worker = isolated_process(_outcome_worker, (url, timeout_seconds, max_content_length, out_queue))
    worker.start()
    join_or_kill(worker, join_timeout, f"the genuine cffi fetcher on {url}")
    if worker.exitcode != 0:
        pytest.fail(f"the cffi worker crashed: exitcode={worker.exitcode} for {url}")
    if out_queue.empty():
        pytest.fail(f"the cffi worker produced no outcome for {url}")
    return out_queue.get()


__all__ = [
    "CffiFetchOutcome",
    "isolated_process",
    "isolated_queue",
    "MockResponse",
    "allow_scripted_egress",
    "dispatching_transport_client",
    "patch_http_fetcher_transport",
    "real_cffi_fetch_outcome",
    "ScriptedClient",
    "scripted_transport_client",
]
