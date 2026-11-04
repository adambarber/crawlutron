"""The one way a test obtains the service's ASGI app: ``crawlutron.main`` declares the
global as ``FastAPI | None`` (right for a module that may lack FastAPI), but a
caller needs a real app, so the narrowing lives here once instead of per site.
"""

from fastapi import FastAPI


def service_app() -> FastAPI:
    """The module-level ASGI app, proven present; without FastAPI the module builds
    no app, and reaching through ``None`` would surface a confusing AttributeError
    several frames from the real cause.
    """
    from crawlutron.main import app

    assert app is not None, (
        "crawlutron.main built no ASGI app -- FastAPI is not importable in this environment"
    )
    return app


def fresh_app() -> FastAPI:
    """A NEWLY constructed app (create_app), not the cached module-level one."""
    from crawlutron.main import create_app

    return create_app()
