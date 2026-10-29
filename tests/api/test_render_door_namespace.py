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
from tests._async_harnesses import issue_key
from tests._quota_harness import service_client


class _HarvestingContext:
    """A page context whose cookie jar holds the session the render minted."""

    async def cookies(self, url=None):
        return [{"name": "sess", "value": "render-secret"}]

    async def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass


class _HarvestingRenderer:
    """A pool-bound renderer double that runs the real harvest, then renders."""

    async def render(self, url, options, *, policy=None):
        from crawlutron.transport.browser_cookies import harvest_session_cookies

        page = SimpleNamespace(context=_HarvestingContext())
        await harvest_session_cookies(page, url)
        attempt = SimpleNamespace(strategy=None)
        return SimpleNamespace(
            status="success", classification="rendered", html=None, text="t", attempt=attempt
        )


class _HarvestingPool:
    """A minimal pool double handing out the harvesting renderer."""

    browser_strategy = "browser_headless"

    async def acquire(self, timeout=None):
        return _HarvestingRenderer()

    def release(self, renderer) -> None:
        pass

    async def close(self) -> None:
        pass


@contextlib.asynccontextmanager
async def _render_client(tmp_path: Path):
    """The render/keys app wired to a browser pool serving one harvesting page."""
    from crawlutron.transport.browser_pool import set_browser_pool

    previous = set_browser_pool(_HarvestingPool())
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

    browser_cookies.reset_process_state()
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
        browser_cookies.reset_process_state()
