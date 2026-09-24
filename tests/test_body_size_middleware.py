"""ASGI body-size middleware: cap, truncation and chunked transfer handling.

When the client sends a Transfer-Encoding: chunked request with multiple
``http.request`` messages, the middleware must:
1. Accept the first chunk(s) under the cap are delivered intact.
2. After truncation, ``Connection: close`` is set on the response.
"""

import inspect

from _assertions import extract_function_body


def _read_main_source() -> str:

    from app import main as main_mod

    return inspect.getsource(main_mod)





def test_chunked_request_under_cap_is_not_rejected_pre_emptively():
    """The Content-Length pre-check must reject only when CL > cap.
    A small CL body is allowed through to the wrapped receive() path
    (adversarial review F-no-body-size-limit)."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    assert "int(content_length) > _MAX_REQUEST_BODY_BYTES" in source


def test_chunked_request_truncation_sets_connection_close():
    """Truncated requests must have Connection: close so keep-alive
    sockets are not poisoned (adversarial review F-no-body-size-limit C1)."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    assert 'response.headers["Connection"] = "close"' in source


def test_chunked_request_drain_continues_after_truncation():
    """After truncation the receive() wrapper must keep draining
    original_receive() so the ASGI server's socket buffer empties, and must
    never admit another byte (B5: ``truncated`` is sticky)."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    body = extract_function_body(source, "_capped_receive")
    # The wrapper still awaits the underlying receive on every call (drain), ...
    assert "message = await original_receive()" in body
    # ... and short-circuits at the top once the cap was breached.
    assert "if truncated:" in body


def test_chunked_request_cap_is_16_mib():
    """The cap must be exactly 16 MiB (16 * 1024 * 1024)."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    assert "_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024" in source


class TestBodySizeMiddlewareRegistration:
    def test_body_size_middleware_registered(self):
        """The middleware must be installed on the FastAPI app."""
        src = _read_main_source()
        # Registration lives in main.py; implementation lives in app/api/middleware.py
        assert "limit_request_body" in src
        assert 'app.middleware("http")(limit_request_body)' in src or '@app.middleware("http")' in src

        from app.api import middleware as mw_mod

        mw_src = inspect.getsource(mw_mod)
        assert "async def limit_request_body" in mw_src or "async def _limit_request_body" in mw_src
        # Must wrap the ASGI receive() rather than buffer-and-replay
        assert "original_receive" in mw_src
        assert "request._receive = _capped_receive" in mw_src


class TestBodySizeTruncation:
    def test_body_size_middleware_sets_connection_close_on_truncation(self):
        src_path = "app/api/middleware.py"
        with open(src_path) as f:
            source = f.read()
        assert 'response.headers["Connection"] = "close"' in source
        assert "if truncated:" in source

    def test_body_size_middleware_drains_after_truncation(self):
        src_path = "app/api/middleware.py"
        with open(src_path) as f:
            source = f.read()
        # The wrapper keeps awaiting the underlying receive() after the cap is hit
        # (so a reading app keeps consuming the socket) instead of caching a final
        # message, and every post-cap receive hands the app an empty final chunk.
        body = extract_function_body(source, "_capped_receive")
        assert "message = await original_receive()" in body
        assert "if truncated:" in body

    def test_body_size_middleware_preserves_per_chunk_headers(self):
        src_path = "app/api/middleware.py"
        with open(src_path) as f:
            source = f.read()
        body = extract_function_body(source, "_capped_receive")
        non_comment = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        assert "{**message" in non_comment
        assert "dict(message)" not in non_comment

    def test_body_size_middleware_truncation_keeps_more_body_false(self):
        src_path = "app/api/middleware.py"
        with open(src_path) as f:
            source = f.read()
        body = extract_function_body(source, "_capped_receive")
        assert '"more_body": False' in body
