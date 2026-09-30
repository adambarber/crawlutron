"""ASGI body-size middleware: cap, truncation and chunked transfer handling.

When the client sends a Transfer-Encoding: chunked request with multiple
``http.request`` messages, the middleware must:
1. Accept the first chunk(s) under the cap are delivered intact.
2. After truncation, ``Connection: close`` is set on the response.
"""

import inspect

from tests._assertions import extract_function_body, read_main_source


def _read_middleware_source() -> str:
    """``app/api/middleware.py``'s source, read through the module object.

    Not ``open("app/api/middleware.py")``: a relative path is a CWD assumption,
    and this suite is run from the service root only by convention.
    """
    from app.api import middleware as mw_mod

    return inspect.getsource(mw_mod)


def _read_capped_receive() -> str:
    """Just the ``_capped_receive`` wrapper, which is where the cap lives."""
    return extract_function_body(_read_middleware_source(), "_capped_receive")


def test_chunked_request_under_cap_is_not_rejected_pre_emptively():
    """The Content-Length pre-check must reject only when CL > cap.
    A small CL body is allowed through to the wrapped receive() path."""
    source = _read_middleware_source()
    assert "int(content_length) > _MAX_REQUEST_BODY_BYTES" in source


def test_chunked_request_truncation_sets_connection_close():
    """Truncated requests must have Connection: close so keep-alive
    sockets are not poisoned."""
    source = _read_middleware_source()
    assert 'response.headers["Connection"] = "close"' in source


def test_chunked_request_drain_continues_after_truncation():
    """After truncation the receive() wrapper must keep draining
    original_receive() so the ASGI server's socket buffer empties, and must
    never admit another byte (B5: ``truncated`` is sticky)."""
    body = _read_capped_receive()
    # The wrapper still awaits the underlying receive on every call (drain), ...
    assert "message = await original_receive()" in body
    # ... and short-circuits at the top once the cap was breached.
    assert "if truncated:" in body


def test_chunked_request_cap_is_16_mib():
    """The cap must be exactly 16 MiB (16 * 1024 * 1024)."""
    assert "_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024" in _read_middleware_source()


class TestBodySizeMiddlewareRegistration:
    def test_body_size_middleware_registered(self):
        """The middleware must be installed on the FastAPI app."""
        src = read_main_source()
        # Registration lives in main.py; implementation lives in app/api/middleware.py
        assert "limit_request_body" in src
        assert 'app.middleware("http")(limit_request_body)' in src or '@app.middleware("http")' in src

        from app.api import middleware as mw_mod

        mw_src = inspect.getsource(mw_mod)
        assert "async def limit_request_body" in mw_src or "async def _limit_request_body" in mw_src
        # Must wrap the ASGI receive() rather than buffer-and-replay
        assert "original_receive" in mw_src
        assert "request._receive = _capped_receive" in mw_src


def test_body_size_middleware_preserves_per_chunk_headers():
    non_comment = "\n".join(
        line for line in _read_capped_receive().splitlines() if not line.lstrip().startswith("#")
    )
    assert "{**message" in non_comment
    assert "dict(message)" not in non_comment


def test_body_size_middleware_truncation_keeps_more_body_false():
    assert '"more_body": False' in _read_capped_receive()
