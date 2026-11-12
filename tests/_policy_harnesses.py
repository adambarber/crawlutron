"""An app over the crawl and policy routers with stores the test owns.

The policy rollup reads EVERY retained record, so a count-asserting test cannot
share the service's stores -- another test in the worker moves the numbers.
"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.json_value import JsonValue
from crawlutron_rest.api.caller_auth import get_caller_identity
from crawlutron_rest.api.crawl_router import router as crawl_router
from crawlutron_rest.api.dependencies import get_result_store
from crawlutron_rest.api.policy_router import router as policy_router
from tests._app_harnesses import harness_app
from tests._dependency_overrides import bind_base_overrides


def policy_client(
    result_store,
    job_store,
    *,
    caller: CallerIdentity | None = None,
    key_admin_token: str | None = None,
) -> AsyncClient:
    """An AsyncClient over ``/v1/crawl`` and ``/v1/policy-suggestions``, its error
    handlers read off the real app rather than restated -- the envelope is part of
    the surface, and a harness that spelled it would answer with the old shape.
    """
    app = harness_app(crawl_router, policy_router)
    app.dependency_overrides[get_result_store] = lambda: result_store
    bind_base_overrides(app, job_store=job_store, shutdown_event=asyncio.Event())
    if caller is not None:
        app.dependency_overrides[get_caller_identity] = lambda: caller
    if key_admin_token is not None:
        app.state.crawler_settings = CrawlerSettings.from_mapping(
            {"CRAWLUTRON_KEY_ADMIN_TOKEN": key_admin_token}
        )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def stored_trace(request_id: str, url: str) -> JsonValue:
    """A minimal retained row that parses as a blocked TraceResponse."""
    page: dict[str, JsonValue] = {
        "normalized_url": url,
        "status": "blocked",
        "classification": "access_denied",
        "selected_strategy": None,
        "attempted_strategies": [],
        "classification_evidence": [],
    }
    return {"request_id": request_id, "results": [page]}


__all__ = ["policy_client", "stored_trace"]
