"""The one ASGI Request builder the body-cap suites share: every test spelled the
same scope and receive-closure, and the copies drifted (jscpd clone-pairs).
"""

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

type _Messages = list[dict[str, object]]


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
