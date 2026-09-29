"""The one way a test obtains the service's ASGI app.

``app.main`` declares the module global as ``FastAPI | None``, and that is the
right type for the module: FastAPI is an optional import there so the MCP/CLI
surface and the settings-and-pool wiring can be imported without a web server.
It is the wrong type for a caller, because a caller that reaches for
``app.state`` or ``app.router`` needs a real app and cannot do anything useful
with ``None``.

Rather than re-assert that at each of the call sites -- which is how a test ends
up either indexing an optional or, worse, ``assert app``-ing its way past a
genuinely absent app one line at a time -- the check lives here once and returns
the narrowed type.
"""

from fastapi import FastAPI


def service_app() -> FastAPI:
    """The module-level ASGI app, proven present.

    The assertion is not a formality: without FastAPI installed the module
    deliberately builds no app, and a test that then reached through ``None``
    would report a confusing ``AttributeError`` several frames from the cause.
    """
    from app.main import app

    assert app is not None, "app.main built no ASGI app -- FastAPI is not importable in this environment"
    return app
