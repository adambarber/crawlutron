"""An app over the crawl and policy routers with stores the test owns.

The policy rollup reads EVERY retained record, so a test that asserts counts
cannot share the service's own stores: another test in the same worker that
crawls the same session server moves the numbers under it. This app owns its
result store, and takes the job store and the event so the crawl door behind
the same overrides is the real one.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.crawl_router import router as crawl_router
from app.api.dependencies import (
    get_job_store,
    get_result_store,
    get_settings,
    get_shutdown_event,
)
from app.api.policy_router import router as policy_router
from app.config.settings import CrawlerSettings
from tests._app_factory import service_app


def policy_client(result_store, job_store) -> AsyncClient:
    """An AsyncClient over ``/v1/crawl`` and ``/v1/policy-suggestions``.

    The error handlers are read off the real app rather than restated: the
    envelope is part of the surface under test, and a harness that spelled the
    handlers out would keep answering with the old shape after the service
    changed.
    """
    app = FastAPI()
    app.include_router(crawl_router)
    app.include_router(policy_router)
    for exception_class, handler in service_app().exception_handlers.items():
        app.add_exception_handler(exception_class, handler)
    app.dependency_overrides[get_result_store] = lambda: result_store
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: asyncio.Event()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


__all__ = ["policy_client"]
