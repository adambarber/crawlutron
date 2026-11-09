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


async def pass_through(request: Request) -> Response:
    """The call_next double that never reads the body."""
    return Response("")
