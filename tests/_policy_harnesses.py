"""An app over the crawl and policy routers with stores the test owns.

The policy rollup reads EVERY retained record, so a count-asserting test cannot
share the service's stores -- another test in the worker moves the numbers.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.crawl_router import router as crawl_router
from app.api.dependencies import get_result_store
from app.api.policy_router import router as policy_router
from tests._app_factory import service_app
from tests._dependency_overrides import bind_base_overrides


def policy_client(result_store, job_store) -> AsyncClient:
    """An AsyncClient over ``/v1/crawl`` and ``/v1/policy-suggestions``, its error
    handlers read off the real app rather than restated -- the envelope is part of
    the surface, and a harness that spelled it would answer with the old shape.
    """
    app = FastAPI()
    app.include_router(crawl_router)
    app.include_router(policy_router)
    for exception_class, handler in service_app().exception_handlers.items():
        app.add_exception_handler(exception_class, handler)
    app.dependency_overrides[get_result_store] = lambda: result_store
    bind_base_overrides(app, job_store=job_store, shutdown_event=asyncio.Event())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


__all__ = ["policy_client"]
