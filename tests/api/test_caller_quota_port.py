"""The caller-quota admission PORT: doors consume the injected interface, not the module.

``crawlutron.service.caller_auth`` is the framework-free admission core; the
doors (REST and MCP) must depend on the :class:`CallerQuotaAdmission` interface
injected at app construction, so a test double substitutes without patching
any module. The wiring site is app.state -- ONE injection point.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION, CallerQuotaAdmission
from tests._async_harnesses import serve
from tests._quota_harness import stores
from tests.factories.client import close_all, post_crawl

if TYPE_CHECKING:
    from httpx import AsyncClient


class _RecordingAdmission:
    """A test double: records every admit, then delegates to the real port."""

    def __init__(self) -> None:
        self.admitted: list[str] = []

    def __getattr__(self, name: str):
        return getattr(CALLER_QUOTA_ADMISSION, name)

    def admit(self, caller: CallerIdentity | None, requested_pages: int, **kwargs):
        self.admitted.append("anonymous" if caller is None or not caller.caller_id else caller.caller_id)
        return CALLER_QUOTA_ADMISSION.admit(caller, requested_pages, **kwargs)


@contextlib.asynccontextmanager
async def _ported_client(
    tmp_path, admission
) -> AsyncGenerator[tuple[AsyncClient, object, object, object], None]:
    """The MCP+crawl+keys harness with ``admission`` substituted for the port."""
    from tests._quota_harness import mcp_crawl_keys_routers, service_client

    async with service_client(
        tmp_path,
        routers=mcp_crawl_keys_routers(),
        with_results=True,
        wire=lambda app: setattr(app.state, "caller_quota_admission", admission),
    ) as (client, store, keys):
        yield client, store, keys, None


async def test_a_substituted_port_answers_the_crawl_door(tmp_path):
    """The crawl door admits through the port wired at app construction: the
    double records the admission, the wire answer is unchanged."""
    double = _RecordingAdmission()
    async with _ported_client(tmp_path, double) as (client, store, results, keys):
        response = await post_crawl(client)
        assert response.status_code in (202, 200), response.text
        assert double.admitted == ["anonymous"]


async def test_a_substituted_port_answers_the_mcp_crawl_door(tmp_path):
    """The MCP crawl door admits through the same injected port."""
    from tests.mcp.test_mcp import _META, _body, _headers

    double = _RecordingAdmission()
    async with _ported_client(tmp_path, double) as (client, store, results, keys):
        body = _body(
            "tools/call",
            params={"name": "crawl", "arguments": {"url": "http://example.com/"}, "_meta": {**_META}},
        )
        response = await client.post("/mcp", json=body, headers=_headers(body))
        assert response.status_code == 200, response.text
        assert "anonymous" in double.admitted, double.admitted


async def test_an_unwired_port_refuses_loudly(tmp_path):
    """A door wired without the port is a wiring defect: the request refuses
    with the named 500, never a silent fallback to a second admission path."""
    import asyncio

    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker

    store, results, keys = stores(tmp_path)
    from crawlutron.api.identity_router import router as identity_router
    from tests._app_harnesses import harness_app

    app = harness_app(identity_router)
    app.state.caller_key_store = keys
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.state.crawler_settings = CrawlerSettings.from_mapping({})
    app.state.shutdown_event = asyncio.Event()
    app.state.job_store = store
    del app.state.caller_quota_admission
    try:
        async with serve(app) as client:
            response = await client.post("/v1/map", json={"url": "http://example.com/"})
            assert response.status_code == 500, response.text
            assert response.json()["code"] == "caller_quota_unwired", response.text
    finally:
        close_all(store, results, keys)


def test_the_port_method_surface_is_exactly_the_door_consumption():
    """The Protocol declares exactly the members the doors consume -- no more."""
    declared = set(getattr(CallerQuotaAdmission, "__annotations__", {})) | {
        name for name, value in vars(CallerQuotaAdmission).items() if not name.startswith("_")
    }
    assert declared == {
        "admit",
        "resolve_page_limit",
        "effective_cookie_namespace",
        "refuse_anonymous_cookie_namespace",
        "anonymous_caller",
        "bound_render_cookie_namespace",
    }


def test_the_port_module_imports_no_fastapi_at_runtime():
    """The port is framework-free: no runtime fastapi import in the core module."""
    import crawlutron.service.caller_auth as core

    tree = ast.parse(pathlib.Path(str(core.__file__)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        else:
            continue
        assert "fastapi" not in roots, f"runtime fastapi import at line {node.lineno} of the port"


def test_the_default_admission_implements_the_port():
    """The named default instance really implements the port; the cast is not the check."""
    assert isinstance(CALLER_QUOTA_ADMISSION, CallerQuotaAdmission)
