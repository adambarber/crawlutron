"""Adversarial fix test: body-size middleware handles chunked transfer encoding.

When the client sends a Transfer-Encoding: chunked request with multiple
``http.request`` messages, the middleware must:
1. Accept the first chunk(s) under the cap are delivered intact.
2. After truncation, ``Connection: close`` is set on the response.
"""


def _extract_function_body(source: str, name: str) -> str:
    """Extract just the body of an async function ``name`` at any indent."""
    import re
    pattern = re.compile(
        r"^[ \t]*async def " + re.escape(name) + r"\b.*?^[ \t]*(?=@app\.middleware|class |def |async def [a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(source)
    if not m:
        raise AssertionError(f"Could not find function {name}")
    return m.group(0)


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
    assert "response.headers[\"Connection\"] = \"close\"" in source


def test_chunked_request_drain_continues_after_truncation():
    """After truncation the receive() wrapper must keep draining
    original_receive() so the ASGI server's socket buffer empties."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    body = _extract_function_body(source, "_capped_receive")
    assert "drain_after_truncate" in body
    assert "already_drained" in body


def test_chunked_request_cap_is_16_mib():
    """The cap must be exactly 16 MiB (16 * 1024 * 1024)."""
    with open("app/api/middleware.py") as f:
        source = f.read()
    assert "_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024" in source
