"""The /v1/render door's cookie namespace (#483).

A keyed caller's render harvest must land in the caller's own namespace, never
the shared default: otherwise an anonymous crawl could replay the session.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

from crawlutron.api.identity_router import router as identity_router
from crawlutron.api.keys_router import router as keys_router
from crawlutron.models.render_options import RenderOptions
from crawlutron.models.taxonomy import Classification
from crawlutron.runtime import process_state
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.browser_pool import BrowserPool
from crawlutron.transport.render_page import RenderPage
from tests._async_harnesses import issue_key
from tests._policy_factories import canonical
from tests._quota_harness import service_client


class _HarvestingContext:
    """A page context whose cookie jar holds the session the render minted."""

    async def cookies(self, url=None):
        return [{"name": "sess", "value": "render-secret"}]

    async def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass


class _HarvestingRenderer(BrowserRenderer):
    """A pool-bound renderer double that runs the real harvest, then renders."""

    def __init__(self) -> None:
        super().__init__(extract=_harvest_then_render)

    async def render(self, url, options, *, policy=None):
        from crawlutron.transport.browser_cookies import harvest_session_cookies

        page = SimpleNamespace(context=_HarvestingContext())
        await harvest_session_cookies(page, url)
        attempt = SimpleNamespace(strategy=None)
        return SimpleNamespace(
            status="success", classification="rendered", html=None, text="t", attempt=attempt
        )


async def _harvest_then_render(
    render_page: RenderPage, _render_options: RenderOptions
) -> tuple[str | None, str | None, Classification, bool]:
    from crawlutron.transport.browser_cookies import harvest_session_cookies

    page = SimpleNamespace(context=_HarvestingContext())
    await harvest_session_cookies(page, canonical(render_page.url))
    return None, "t", "rendered", False


@contextlib.asynccontextmanager
async def _render_client(tmp_path: Path):
    """The render/keys app wired to a browser pool serving one harvesting page."""
    from crawlutron.transport.browser_pool import set_browser_pool

    pool = BrowserPool.for_testing([_HarvestingRenderer()])
    previous = set_browser_pool(pool)
    try:
        async with service_client(tmp_path, routers={"doors": identity_router, "keys": keys_router}) as (
            client,
            _job_store,
            keys,
        ):
            yield client, keys
    finally:
        set_browser_pool(previous)


async def test_keyed_render_harvest_lands_in_the_callers_namespace(tmp_path):
    """A keyed /v1/render stores its harvest under the caller's key id, not ""."""
    from crawlutron.transport import browser_cookies

    process_state.reset_all_process_state()
    try:
        async with _render_client(tmp_path) as (client, keys):
            secret = await issue_key(client)
            key_id = keys.list_keys()[0].key_id
            response = await client.post(
                "/v1/render",
                headers={"X-API-Key": secret},
                json={"urls": ["http://1.2.3.4/"], "options": {"respect_robots": False}},
            )
            assert response.status_code == 200, response.text
            keyed = browser_cookies.get_browser_cookies_for("1.2.3.4", namespace=key_id)
            shared = browser_cookies.get_browser_cookies_for("1.2.3.4", namespace="")
            assert keyed == "sess=render-secret", (keyed, shared)
            assert shared is None, "the keyed render's harvest leaked into the shared slot"
    finally:
        process_state.reset_all_process_state()
