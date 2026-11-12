"""The one way a test obtains the service's ASGI app: ``crawlutron_rest.main`` declares the
global as ``FastAPI | None`` (right for a module that may lack FastAPI), but a
caller needs a real app, so the narrowing lives here once instead of per site.
"""

from fastapi import FastAPI


def service_app() -> FastAPI:
    """The module-level ASGI app, proven present; without FastAPI the module builds
    no app, and reaching through ``None`` would surface a confusing AttributeError
    several frames from the real cause.
    """
    from crawlutron_rest.main import app

    assert app is not None, (
        "crawlutron_rest.main built no ASGI app -- FastAPI is not importable in this environment"
    )
    return app


def fresh_app() -> FastAPI:
    """A NEWLY constructed app (create_app), not the cached module-level one."""
    from crawlutron_rest.main import create_app

    return create_app()


def wired_app(monkeypatch, tmp_path) -> FastAPI:
    """A fresh app pointed at a tmp cache dir with the heavy teardown stubbed:
    the lifespan-wiring setup the cache/spill sweep tests share."""
    from crawlutron_rest.main import create_app
    from tests._lifespan_doubles import stub_heavy_teardown

    monkeypatch.setenv("CRAWLUTRON_CACHE_DIR", str(tmp_path))
    stub_heavy_teardown(monkeypatch)
    return create_app()
