"""The body cap must be enforced without retaining the declared body (#1486)."""

from __future__ import annotations

import tracemalloc

import pytest
from starlette.responses import Response

from crawlutron_rest.api.middleware import limit_request_body
from tests._asgi_request import make_request

WINDOW = 8192
BIG = 8 * 1024 * 1024
CHUNK = 64 * 1024


def _chunks(total: int, size: int, tail: bytes | None = None) -> list[bytes]:
    """A declared body split into bounded chunks, built before any memory clock."""
    parts = [b"x" * size for _ in range(total // size)]
    if total % size:
        parts.append(b"x" * (total % size))
    if tail is not None:
        parts.append(tail)
    return parts


def _stream(parts: list[bytes], declared: int):
    """One declared request whose receive() pops the scripted chunks."""
    messages = [
        {"type": "http.request", "body": part, "more_body": index < len(parts) - 1}
        for index, part in enumerate(parts)
    ]
    return make_request(messages, headers=[(b"content-length", str(declared).encode())])


def _counting_app(seen: list[int]):
    """A reader that consumes every chunk but retains no bytes."""

    async def call_next(request):
        seen.append(0)
        while True:
            message = await request.receive()
            if not message.get("more_body", False):
                break
        return Response("")

    return call_next


async def test_large_declared_body_is_not_buffered() -> None:
    """Kills mutant: _collect_declared_body retaining the whole declared body."""
    remaining = BIG

    async def receive():
        nonlocal remaining
        size = min(CHUNK, remaining)
        remaining -= size
        return {"type": "http.request", "body": b"x" * size, "more_body": remaining > 0}

    request = make_request([], headers=[(b"content-length", str(BIG).encode())], receive=receive)
    seen: list[int] = []
    tracemalloc.start()
    try:
        response = await limit_request_body(request, _counting_app(seen), max_bytes=16 * 1024 * 1024)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert response.status_code == 200
    assert peak < 1024 * 1024, f"buffered {peak} bytes for a {BIG}-byte body"


@pytest.mark.parametrize("declared", [WINDOW - 1, WINDOW, WINDOW + 1])
async def test_both_buffer_window_sides_are_admitted(declared: int) -> None:
    """Kills mutant: an off-by-one window that refuses the admitted side."""
    parts = [b"z" * declared]
    seen: list[int] = []
    response = await limit_request_body(_stream(parts, declared), _counting_app(seen), max_bytes=BIG)
    assert response.status_code == 200


async def test_large_over_declared_chunk_is_split_and_closes() -> None:
    """Kills mutant: a streaming path that forwards bytes past the declaration."""
    declared = 64 * 1024
    parts = [b"y" * (declared + 32), b"", b""]
    seen: list[bytes] = []

    async def call_next(request):
        body = bytearray()
        while True:
            message = await request.receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        seen.append(bytes(body))
        return Response("")

    response = await limit_request_body(_stream(parts, declared), call_next, max_bytes=BIG)
    assert response.status_code == 200
    assert len(seen[0]) == declared
    assert response.headers["Connection"] == "close"


async def test_large_short_body_is_refused_413() -> None:
    """Kills mutant: a streaming path that processes a truncated declaration."""
    declared = 64 * 1024
    request = _stream([b"a" * 1024], declared)

    async def call_next(request):
        while True:
            message = await request.receive()
            if not message.get("more_body", False):
                break
        return Response("")

    response = await limit_request_body(request, call_next, max_bytes=BIG)
    assert response.status_code == 413


async def test_large_conflicting_content_length_refuses_before_the_app() -> None:
    """Kills mutant: a streaming path that trusts whichever duplicate CL it reads."""
    request = make_request(
        [{"type": "http.request", "body": b"a" * 1024, "more_body": False}],
        headers=[(b"content-length", b"65536"), (b"content-length", b"65537")],
    )

    async def call_next(request):
        raise AssertionError("a conflicting framing must never reach the app")

    response = await limit_request_body(request, call_next, max_bytes=BIG)
    assert response.status_code == 413
    assert response.headers["Connection"] == "close"


async def test_hostile_bytes_inside_a_large_body_do_not_break_framing() -> None:
    """Kills mutant: a streaming decoder that treats NUL/CRLF as control frames."""
    declared = 64 * 1024
    parts = [b"\x00\r\n\x00" + b"q" * (declared - 4)]
    seen: list[int] = []
    response = await limit_request_body(_stream(parts, declared), _counting_app(seen), max_bytes=BIG)
    assert response.status_code == 200
