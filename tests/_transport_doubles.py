"""Transport-level test seams: a scripted httpx transport, and the genuine
curl_cffi fetcher in an isolated spawned interpreter (in-process curl_cffi
busy-spins the GIL). Why and how: ``docs/testing.md`` -> Transport doubles."""

import multiprocessing
from collections.abc import Awaitable, Callable, Sequence
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue

import httpx
import pytest

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from tests._fetch_outcome import FetchFailed, FetchOutcome

_ISOLATED = multiprocessing.get_context("spawn")


def isolated_queue() -> "Queue[FetchOutcome]":
    """A queue from the same context as :func:`isolated_process`.

    Its element type is the report both spawned reporters put on it: the one
    declaration of what crosses that boundary."""
    return _ISOLATED.Queue()


def isolated_process(target: Callable[..., None], args: Sequence[object] = ()) -> BaseProcess:
    """A child process that inherits nothing from this test process.

    Named rather than inlined because the start method is the whole point; it
    builds ``BaseProcess``, which the spawn context's factory returns."""
    return _ISOLATED.Process(target=target, args=tuple(args))


class _ScriptedBody(httpx.AsyncByteStream):
    """The bytes a scripted server sent, delivered as a stream.

    ``httpx.Response(content=...)`` reads eagerly and marks the body consumed;
    building from the public ``AsyncByteStream`` says the response is unread."""

    def __init__(self, body: bytes) -> None:
        self._chunks = [body] if body else []

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class MockResponse(httpx.Response):
    """A real ``httpx.Response`` with a positional-friendly constructor.

    Headers are ``httpx.Headers`` (case-insensitive, two ``Set-Cookie`` lines)
    and the body is a stream, so ``aiter_raw()`` reads it as the fetcher does."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None, content: bytes = b"") -> None:
        super().__init__(status_code, headers=headers, stream=_ScriptedBody(content))


def _recording_hook[T: httpx.Request | httpx.Response](sink: list[T]) -> Callable[[T], Awaitable[None]]:
    """An httpx event hook that appends every event to `sink`.

    A coroutine function because httpx *awaits* the result; a hook that returns
    ``None`` fails at the client, not at the test that installed it.
    """

    async def hook(event):
        sink.append(event)

    return hook


class ScriptedClient(httpx.AsyncClient):
    """An ``httpx.AsyncClient`` that also keeps the headers it was called with.

    The type owns ``captured_headers`` rather than a factory poking it on
    after construction."""

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

    ``httpx.MockTransport`` is the seam httpx publishes; a call beyond the
    script raises ``IndexError``; the event hooks record request and response."""
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

    The right shape when the answer depends on where the request went (a
    redirect chain); only the answerer differs from :func:`scripted_transport_client`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def allow_scripted_egress(monkeypatch, ip: str = "1.2.3.4") -> None:
    """Stand the SSRF guard down for a target no resolver could answer for.

    A scripted host has no DNS, so the two network-asking seams stand down;
    everything downstream still runs. ``ip`` is public by construction."""
    from crawlutron.transport import pinned_transport as pinned_mod

    monkeypatch.setattr(pinned_mod, "check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(pinned_mod, "resolve_and_pin_host", lambda url: (url.host, ip))


def patch_http_fetcher_transport(
    monkeypatch,
    responses: Sequence[httpx.Response],
    *,
    allow_ssrf_bypass: bool,
) -> httpx.AsyncClient:
    """Point the httpx fetcher at a scripted transport; return the client.

    ``allow_ssrf_bypass`` is required because installing this also installs
    the egress stand-down, and reaching that by accident voids the verdict."""
    if not allow_ssrf_bypass:
        raise ValueError("patch_http_fetcher_transport requires allow_ssrf_bypass=True")

    from crawlutron.transport import http_client_pool as pool_mod

    client = scripted_transport_client(responses)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)
    allow_scripted_egress(monkeypatch)
    return client


def _genuine_cffi_outcome(url: str, timeout_seconds: int, max_content_length: int) -> FetchOutcome:
    """Fetch `url` with the real fetcher, right here, and classify the result.

    Returns the production result or a :class:`FetchFailed` naming what ended it;
    see ``tests/_fetch_outcome.py`` for why the boundary needs the second arm.
    """
    import asyncio

    from crawlutron.transport.fetchers import impersonating_fetcher
    from tests._policy_factories import canonical

    async def _fetch():
        return await impersonating_fetcher()(
            canonical(url),
            {},
            timeout_seconds,
            max_content_length,
        )

    try:
        return asyncio.run(_fetch())
    except Exception as failure:
        return FetchFailed(error=type(failure).__name__, detail=str(failure)[:120])


def outcome_worker(
    url: str, timeout_seconds: int, max_content_length: int, out_queue: Queue[FetchOutcome]
) -> None:
    """Subprocess body: hand the parent one outcome and exit.

    Module-level because a spawn target must be importable in the child, and
    separate from the classification the parent asserts on."""
    out_queue.put(_genuine_cffi_outcome(url, timeout_seconds, max_content_length))


def join_or_kill(worker: BaseProcess, join_timeout: float, what: str) -> None:
    """Wait ``join_timeout`` for ``worker``; kill it and fail if still running.

    A bare ``join(timeout)`` returns and the wedged child keeps running; written
    per test, the terminate step is the part that gets dropped."""
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
) -> FetchOutcome:
    """Run the genuine curl_cffi fetcher against `url` in a spawned interpreter.

    Loopback targets set ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1`` here;
    ``join_timeout`` is a hang guard, not a budget."""
    out_queue = isolated_queue()
    worker = isolated_process(outcome_worker, (url, timeout_seconds, max_content_length, out_queue))
    worker.start()
    join_or_kill(worker, join_timeout, f"the genuine cffi fetcher on {url}")
    if worker.exitcode != 0:
        pytest.fail(f"the cffi worker crashed: exitcode={worker.exitcode} for {url}")
    if out_queue.empty():
        pytest.fail(f"the cffi worker produced no outcome for {url}")
    return out_queue.get()


__all__ = [
    "FetchOutcome",
    "HttpFetchResult",
    "isolated_process",
    "isolated_queue",
    "join_or_kill",
    "MockResponse",
    "allow_scripted_egress",
    "dispatching_transport_client",
    "outcome_worker",
    "patch_http_fetcher_transport",
    "real_cffi_fetch_outcome",
    "ScriptedClient",
    "scripted_transport_client",
]
