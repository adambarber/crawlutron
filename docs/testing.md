# Testing design notes

Notes on the test suite's structural decisions — the "why" behind a harness,
so the code next to a decision can stay short.

## Transport doubles

`tests/_transport_doubles.py` provides the two transport boundaries the
fetchers cross: an HTTP path and a cffi path.

**The HTTP path is replaced, not imitated.** The client is a real
`httpx.AsyncClient` over `httpx.MockTransport`, and responses are real
`httpx.Response` objects carrying a real async body stream — so the fetcher's
own call (`async with client.stream(...) as response: async for chunk in
response.aiter_raw()`) is production code with the production timeout and
redirect chain. The double this replaced had a plain `dict` for headers (no
case-insensitive lookup, no duplicates) and a `stream()` that returned a
response instead of an async context manager; neither could break when the
fetcher changed how it reads a response, which is the only thing a transport
double is for. A scripted response beyond the list raises `IndexError` rather
than repeating an answer, because a fetch that asked for more hops than the
script has is a fact about the test.

**The cffi path runs a real spawned interpreter.** In-process curl_cffi
busy-spins the GIL under fast sleeps and wedges the suite, so the genuine
fetcher runs in a child process with a hard join (`real_cffi` marker /
`conftest.stub_cffi_fetcher`). `real_cffi_fetch_outcome` owns the child, the
queue, the join, the terminate; `join_or_kill` is the hang guard every
spawned test needs (a bare `join(timeout)` returns and the wedged child keeps
running). The child inherits the parent's environment, so loopback targets
set `CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1` in the parent and undo it in
`monkeypatch`.

**"Spawned" is a harness decision, not a platform default.** The default
start method is `fork` on Linux and `spawn` on macOS. A fork child copies the
patched stubs and zeroed `time.sleep`, so isolation silently did not happen
on fork platforms until `isolated_process` named the start method — the one
place that decision lives. It builds a `BaseProcess` (`spawn` returns
`SpawnProcess`, a sibling of `multiprocessing.Process`) and `isolated_queue`
agrees on the multiprocessing context.

## Waiting

The suite replaces wall-clock sleeps with zero-duration events or injected
clocks (see `conftest.fast_sleeps`); tests that genuinely wait poll a
predicate off-loop or off-process rather than sleeping.

## Why this doc exists

The engineering notes behind a test decision live here. Module docstrings
stay short and point at this doc; a decision's *rationale* belongs in one
place a reviewer can read, not re-derived at every use site.