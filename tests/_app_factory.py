"""The one way a test obtains the service's ASGI app: ``app.main`` declares the
global as ``FastAPI | None`` (right for a module that may lack FastAPI), but a
caller needs a real app, so the narrowing lives here once instead of per site.
"""

from fastapi import FastAPI


def service_app() -> FastAPI:
    """The module-level ASGI app, proven present; without FastAPI the module builds
    no app, and reaching through ``None`` would surface a confusing AttributeError
    several frames from the real cause.
    """
    from app.main import app

    assert app is not None, "app.main built no ASGI app -- FastAPI is not importable in this environment"
    return app
