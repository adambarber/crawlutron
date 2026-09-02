# Adversarial Code Review — Round 3

Scope: per-request cookie namespace (Fix A), `_PENDING_CLIENT_CLOSE` bounded growth (Fix B), FastAPI body-size middleware (Fix C).

## Fix A — Per-request cookie namespace (`app/crawl/engine.py:736-756`)

**VERIFIED CORRECT.** The `try/finally` around `_crawl_request_impl` properly captures the `_cookie_token` from `_COOKIE_NAMESPACE.set(...)` and resets it on every exit path (return, raise, cancellation). The `.set()` call sits *before* the `try`, so any exception during value extraction (`cookie_namespace_for(...)`) does not require a reset. `render_request` in `app/service.py:99` does not need the namespace — it routes only through the browser worker (`BrowserRenderer`), never through the HTTP cookie-jar helpers in `http.py`. No leakage path was found.

## Fix B — `_PENDING_CLIENT_CLOSE` bounded growth (`app/crawl/strategies/http.py:254-300`)

### Finding B1 — HIGH
- **file:line:** `app/crawl/strategies/http.py:286-292` (drop-oldest branch in `get_async_http_client`)
- **Description:** When the pending-queue cap is reached, the code pops the oldest client and calls `asyncio.ensure_future(_safe_aclose(stale))` *without awaiting it*. `ensure_future` returns a fire-and-forget task; if the current event loop exits before the task runs to completion, the task is cancelled and `stale.aclose()` never executes — leaking the underlying `httpx.HTTPConnection` pool and any open sockets. This is **the same leak class the fix was supposed to eliminate**; the only thing the cap-on-enqueue buys you is a bounded list of references. If the drop happens during `asyncio.run` teardown (the exact scenario `_drain_pending_client_closes` was added to handle), the dropped client's sockets are leaked until GC pressure triggers `httpx.AsyncClient.__del__`, which is itself a best-effort path that can close on a dead loop.
- **Suggested fix:** Replace `asyncio.ensure_future(_safe_aclose(stale))` with a synchronous-safe close: try `client.aclose()` is impossible (it's a coroutine), so instead track the dropped client for the *next* `_drain_pending_client_closes` invocation explicitly, or — simpler — when the cap is hit, just *skip* appending the new client (let it be GC'd) AND call `client.close()` synchronously to release the httpcore connection pool. `httpx.AsyncClient` exposes a sync `close()` that releases transport resources without awaiting the async teardown path.

### Finding B2 — MEDIUM
- **file:line:** `app/crawl/strategies/http.py:286-292` (same branch as B1)
- **Description:** The comment claims "dropping is safe and avoids the leak-recovery path itself becoming a memory leak." That is only true when `ensure_future` succeeds — i.e. a live loop is available. In that case the task holds its own strong reference and the dropped queue entry is fine. But when `RuntimeError` is raised (no running loop), `aclose` is *never* scheduled and the comment is wrong: dropping the reference leaks the client immediately. The current code swallows this with `except RuntimeError: pass`, so the drop branch quietly leaks in exactly the loop-absence scenario the whole bounded queue was meant to defend against.
- **Suggested fix:** When `RuntimeError` is hit, do not pop-and-leak; either leave the cap-overflow client on the queue (reject the new enqueue), or call `stale.close()` synchronously as a fallback before dropping.

### Finding B3 — LOW
- **file:line:** `app/crawl/strategies/http.py:267-277` (cap check before append)
- **Description:** The "already-present" guard (`if old_client not in _PENDING_CLIENT_CLOSE`) runs *outside* the cap check. If the same client is appended twice (shouldn't happen, but the cap path can race with re-entry from a second fetch on the new loop), the cap check fires twice on the same queue length, popping two distinct "oldest" entries and potentially leaving the duplicate `old_client` appended out of order. Cosmetic but contributes to the same leak shape as B1.
- **Suggested fix:** Move the dedupe check inside the cap branch or compute `not in` and `len(...)` under the same lock acquisition.

## Fix C — FastAPI body-size middleware (`app/main.py:13934-14990`)

### Finding C1 — HIGH
- **file:line:** `app/main.py:14970-14985` (`_capped_receive` truncation branch)
- **Description:** After truncation, `_capped_receive` returns `{"type": "http.request", "body": b"", "more_body": False}` *without calling `original_receive()`*. For HTTP/1.1 chunked transfer encoding, the ASGI server still has the remaining chunks queued for delivery to the app. The app side (Starlette/FastAPI) treats `more_body=False` as end-of-body, so it stops calling `receive()` for body. **However the underlying socket still holds those un-acked chunks until the response is sent.** On keep-alive connections a follow-up request on the same socket cannot be parsed until the server's receive-side buffer drains. With uvicorn's default h11 implementation this manifests as the NEXT request on the same connection returning a 400 or hanging — i.e. **one oversized request poisons the keep-alive connection for subsequent legitimate requests.** The middleware should drain (and discard) the remaining receive messages before returning, or set `Connection: close` on the response when truncation occurred.
- **Suggested fix:** After truncating, continue to call and discard `original_receive()` until the ASGI server signals end-of-body, OR set `Connection: close` on the JSONResponse when `truncated` becomes true so the server tears the socket down cleanly.

### Finding C2 — MEDIUM
- **file:line:** `app/main.py:13957-13972` (Content-Length pre-check)
- **Description:** When `Content-Length` is missing entirely, the middleware proceeds to wrap `receive()` — correct. When `Content-Length` is present but unparseable (`ValueError` is caught and ignored), the wrap is also applied — correct. But when `Content-Length` is present and *lies* (e.g., advertises 1 KiB but the actual chunked body is 100 MiB), the pre-check passes, then the wrapper caps at 16 MiB. The 84 MiB of "extra" chunks still flow through the ASGI server's receive pipeline, consuming server-side memory and socket buffers before the wrapper can drop them. The cap is on *delivered-to-FastAPI* bytes, not on bytes received from the wire — the OOM vector is partially mitigated but not eliminated for lying-CL chunked uploads.
- **Suggested fix:** The `httpx`/uvicorn receive path buffers chunks server-side too; the only real fix is to enforce an inbound byte rate limit at the ASGI server level (e.g. nginx `client_max_body_size` or uvicorn's `--limit-max-requests` doesn't apply — need a per-connection byte counter). For an in-app fix, at minimum reject when CL exceeds the cap even if the cap check passed; the current code already does that. But for missing/lying CL, consider reading from the receive channel until the cap is hit, then call `request._scope["headers"] = ...` to disable keep-alive, or return 413 immediately when the running byte count exceeds CL by any amount (CL-vs-actual mismatch is itself a request smuggling signal).

### Finding C3 — MEDIUM
- **file:line:** `app/main.py:14960-14966` (truncation message construction)
- **Description:** `_capped_receive` does `message = dict(message)` and overwrites `body` and `more_body` — but ASGI's `http.request` message also carries `headers` (per-chunk trailers in HTTP/1.1 chunked encoding). Mutating the message with `dict(message)` drops the headers dict entirely, which downstream consumers may not expect. For Starlette this is benign (it only reads `body` and `more_body`), but for ASGI conformance the message should retain its original keys.
- **Suggested fix:** Build the new message as `{**message, "body": body[:remaining], "more_body": False}` instead of `dict(message)`.

### Finding C4 — LOW
- **file:line:** `app/main.py:14970-14978` (`remaining <= 0` branch)
- **Description:** Once the cap is reached, every subsequent `receive()` call returns the empty body without ever pulling from `original_receive()`. Combined with Finding C1, this means an attacker sending a 1 GiB chunked body of which only 16 MiB is "delivered" still has 1008 MiB of socket-buffered data the server expects the app to drain. The cap stops FastAPI from consuming it, but the OS socket buffer (and any upstream proxy buffers) still hold it. The middleware should at minimum `await original_receive()` and discard to give the server back-pressure feedback, even though the bytes are dropped.

