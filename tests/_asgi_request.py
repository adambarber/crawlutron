"""The one ASGI Request builder the body-cap suites share: every test spelled the
same scope and receive-closure, and the copies drifted (jscpd clone-pairs).
"""

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

type _Messages = list[dict[str, object]]


def body_reader_call_next(seen: dict[str, bytes], *, probe: bool = True):
    """A call_next double that reads the whole body into ``seen['body']``;
    ``probe`` (default) also consumes one trailing chunk after ``more_body``
    -- the shape of a streaming reader that checks for a late chunk."""

    async def call_next(req: Request) -> Response:
        body = bytearray()
        while True:
            message = await req.receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        if probe:
            extra = await req.receive()
            body += extra.get("body", b"")
        seen["body"] = bytes(body)
        return Response("")

    return call_next


def make_request(messages: _Messages, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    """A Request whose receive() pops the scripted ASGI message list."""

    async def receive() -> Message:
        return messages.pop(0)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers or [],
        "query_string": b"",
    }
    return Request(scope, receive=receive)


def two_chunk_request(headers: list[tuple[bytes, bytes]], body: bytes = b"Z" * 40) -> Request:
    """A request carrying one body chunk plus the terminator message -- the
    arrange the declared-body refusal suites share (drainable, not empty)."""
    return make_request(
        [
            {"type": "http.request", "body": body, "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False},
        ],
        headers=headers,
    )


def refuse_appends_to(ran: list[Request]):
    """A call_next double that records invocation -- the app-must-not-run
    assertion reads the list empty afterward."""

    async def call_next(req: Request) -> Response:
        ran.append(req)
        return Response("")

    return call_next


def refuse_never(caller):
    """A call_next double whose invocation fails the test immediately."""

    async def call_next(req: Request) -> Response:
        raise AssertionError(caller)

    return call_next


async def pass_through(request: Request) -> Response:
    """The call_next double that never reads the body."""
    return Response("")
