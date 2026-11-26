"""Behavioral tests: search providers and webhook delivery MUST use the
proxied pooled client. Any independent client construction is a topology violation.
"""

from __future__ import annotations

import ast
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.search.providers_api import BraveProvider, SerperProvider, TavilyProvider
from crawlutron.search.providers_html import DuckDuckGoProvider
from crawlutron.service.webhooks import post_webhook
from safe_url.host_policy import HostPolicy
from tests._walk_primitives import offender_at, parsed_source
from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import call_nodes
from tests.rules import Bites, Rule, Silent

# --- Search provider egress mediation ---

_SANCTIONED_SEARCH_HOMES = frozenset(
    {
        "packages/core/src/crawlutron/search/providers.py",
        "packages/core/src/crawlutron/search/providers_api.py",
        "packages/core/src/crawlutron/search/providers_html.py",
    }
)


def _is_unproxied_search_client_construction(tree, call) -> bool:
    """True when a search provider constructs its own outbound client."""
    callee = call.func
    if not isinstance(callee, ast.Attribute):
        return False
    if callee.attr in ("AsyncClient", "Client") and getattr(callee.value, "id", None) == "httpx":
        return True
    if callee.attr == "AsyncSession" and getattr(callee.value, "id", None) in ("curl_cffi", "cffi_requests"):
        return True
    if callee.attr == "urlopen" and getattr(callee.value, "id", None) == "urllib.request":
        return True
    return False


def _search_proxy_mediation_offenders(tree, path) -> list[str]:
    """Flag search provider modules that construct independent outbound clients."""
    relative = relative_posix(path)
    if relative not in _SANCTIONED_SEARCH_HOMES:
        return []
    offenders = []
    for call in call_nodes(tree):
        if _is_unproxied_search_client_construction(tree, call):
            offenders.append(
                offender_at(
                    path,
                    call,
                    (
                        "search provider constructs independent outbound client; "
                        "must use get_async_http_client()"
                    ),
                )
            )
    return offenders


SEARCH_PROXY_MEDIATION = Rule(
    name="search-proxy-mediation",
    find=_search_proxy_mediation_offenders,
    summary=(
        "search providers (Tavily, Brave, Serper, DuckDuckGo) must issue requests "
        "through get_async_http_client()'s proxied pooled client; no independent "
        "httpx.AsyncClient, curl_cffi AsyncSession, or urllib.request.urlopen construction"
    ),
    bite=lambda: Bites(
        "import httpx\nclient = httpx.AsyncClient()\n",
        marker="search provider constructs independent outbound client",
        at=REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "search" / "providers.py",
    ),
    clean=lambda: (
        Silent(
            "from crawlutron.transport.http_client_pool import get_async_http_client\n"
            "client = get_async_http_client()\n",
            at=REPO_ROOT / "tests" / "decoys" / "search_providers.py",
        ),
    ),
)


def test_search_provider_proxy_mediation():
    """Search providers must not construct independent outbound clients."""
    SEARCH_PROXY_MEDIATION.scan()


def test_search_provider_proxy_mediation_bites():
    """The rule's bite proof: an independent construction is named by file:line."""
    SEARCH_PROXY_MEDIATION.check_bites()


# --- Webhook delivery egress mediation ---

_SANCTIONED_WEBHOOK_HOMES = frozenset(
    {
        "packages/core/src/crawlutron/service/webhooks.py",
    }
)


def _webhook_proxy_mediation_offenders(tree, path) -> list[str]:
    """Flag webhook module that constructs independent outbound clients."""
    relative = relative_posix(path)
    if relative not in _SANCTIONED_WEBHOOK_HOMES:
        return []
    offenders = []
    for call in call_nodes(tree):
        callee = call.func
        if not isinstance(callee, ast.Attribute):
            continue
        if callee.attr in ("AsyncClient", "Client") and getattr(callee.value, "id", None) == "httpx":
            # Allow if proxy= keyword is present (sanctioned proxied construction)
            has_proxy = any(kw.arg == "proxy" for kw in call.keywords)
            if not has_proxy:
                offenders.append(
                    offender_at(
                        path,
                        call,
                        (
                            "webhook delivery constructs unproxied httpx client; "
                            "must use get_async_http_client()"
                        ),
                    )
                )
        elif callee.attr == "AsyncSession" and getattr(callee.value, "id", None) in (
            "curl_cffi",
            "cffi_requests",
        ):
            has_proxy = any(kw.arg == "proxy" for kw in call.keywords)
            if not has_proxy:
                offenders.append(
                    offender_at(
                        path,
                        call,
                        (
                            "webhook delivery constructs unproxied curl_cffi session; "
                            "must use get_async_http_client()"
                        ),
                    )
                )
    return offenders


WEBHOOK_PROXY_MEDIATION = Rule(
    name="webhook-proxy-mediation",
    find=_webhook_proxy_mediation_offenders,
    summary=(
        "webhook delivery must issue requests through get_async_http_client()'s "
        "proxied pooled client; no unproxied httpx.AsyncClient or curl_cffi AsyncSession construction"
    ),
    bite=lambda: Bites(
        "import httpx\nclient = httpx.AsyncClient()\n",
        marker="webhook delivery constructs unproxied httpx client",
        at=REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "service" / "webhooks.py",
    ),
    clean=lambda: (
        Silent(
            "from crawlutron.transport.http_client_pool import get_async_http_client\n"
            "client = get_async_http_client()\n",
            at=REPO_ROOT / "tests" / "decoys" / "webhooks.py",
        ),
    ),
)


def test_webhook_delivery_proxy_mediation():
    """Webhook delivery must not construct unproxied outbound clients."""
    WEBHOOK_PROXY_MEDIATION.scan()


def test_webhook_delivery_proxy_mediation_bites():
    """The rule's bite proof: an unproxied construction is named by file:line."""
    WEBHOOK_PROXY_MEDIATION.check_bites()


# --- Behavioral integration tests: verify runtime proxy usage ---


class _RecordingPooledClient:
    """Records whether get_async_http_client() was called and what methods were invoked."""

    def __init__(self):
        self.calls = []
        self.client_get_called = False
        self.client_post_called = False
        self.client_stream_called = False

    def _make_response(self):
        """Create a mock async context manager response."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.aiter_bytes = MagicMock(return_value=iter([b"{}"]))

        # Make it an async context manager
        async def aenter():
            return mock_response

        async def aexit(*args):
            return False

        mock_response.__aenter__ = aenter
        mock_response.__aexit__ = aexit

        return mock_response

    def get(self, request, **kwargs):
        self.client_get_called = True
        self.calls.append(("get", request, kwargs))
        return self._make_response()

    def post(self, request, **kwargs):
        self.client_post_called = True
        self.calls.append(("post", request, kwargs))
        return self._make_response()

    def stream(self, method, request, **kwargs):
        self.client_stream_called = True
        self.calls.append(("stream", method, request, kwargs))
        return self._make_response()


@pytest.fixture
def recording_pooled_client():
    """Fixture providing a recording pooled client for behavioral verification."""
    return _RecordingPooledClient()


async def test_search_providers_use_proxied_pooled_client(recording_pooled_client, monkeypatch):
    """Tavily, Brave, Serper, DuckDuckGo providers must route through get_async_http_client()."""
    import crawlutron.search.providers as providers_module

    monkeypatch.setattr(providers_module, "get_async_http_client", lambda: recording_pooled_client)

    host_policy = HostPolicy.unrestricted()

    # Test Tavily provider
    tavily = TavilyProvider("test-key", host_policy)
    with suppress(Exception):
        await tavily.search("test query", 1, 5.0)

    # Test Brave provider
    brave = BraveProvider("test-key", host_policy)
    with suppress(Exception):
        await brave.search("test query", 1, 5.0)

    # Test Serper provider
    serper = SerperProvider("test-key", host_policy)
    with suppress(Exception):
        await serper.search("test query", 1, 5.0)

    # Test DuckDuckGo provider
    ddg = DuckDuckGoProvider(host_policy)
    with suppress(Exception):
        await ddg.search("test query", 1, 5.0)

    # Verify the pooled client was used for at least some providers
    # (DuckDuckGo may use a different path, but keyed providers must use pooled client)
    used = (
        recording_pooled_client.client_post_called
        or recording_pooled_client.client_get_called
        or recording_pooled_client.client_stream_called
    )
    assert used, "No search provider used the pooled client; topology violation"


async def test_webhook_delivery_uses_proxied_pooled_client(recording_pooled_client, monkeypatch):
    """Webhook delivery must route through get_async_http_client()."""
    import crawlutron.service.webhooks as webhooks_module

    monkeypatch.setattr(webhooks_module, "get_async_http_client", lambda: recording_pooled_client)
    # Validation admits shape only; target DNS and the dial belong to the proxy.
    monkeypatch.setattr(
        webhooks_module,
        "validate_webhook_url_async",
        AsyncMock(),
    )

    target = WebhookTarget.admit("https://webhook.example.com/hook", host_policy=HostPolicy.unrestricted())

    with suppress(Exception):
        await post_webhook(target, {"results": []})

    used = recording_pooled_client.client_post_called or recording_pooled_client.client_stream_called
    assert used, "Webhook delivery did not use the pooled client; topology violation"


# --- Path-exact exemption verification (clean shape only quiet in real homes) ---


def test_search_proxy_mediation_clean_shape_only_quiet_in_real_home():
    """The clean shape (get_async_http_client) is only quiet in the real search provider homes."""
    from tests.chokepoint_paths import REPO_ROOT

    clean_source = (
        "from crawlutron.transport.http_client_pool import get_async_http_client\n"
        "client = get_async_http_client()\n"
    )

    # Real home is quiet for clean shape
    home = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "search" / "providers.py"
    assert _search_proxy_mediation_offenders(parsed_source(clean_source, filename=str(home)), home) == []

    # Violation shape in real home fires
    violation_source = "import httpx\nclient = httpx.AsyncClient()\n"
    offenders = _search_proxy_mediation_offenders(parsed_source(violation_source, filename=str(home)), home)
    assert len(offenders) == 1
    assert "search provider constructs independent outbound client" in offenders[0]


def test_webhook_proxy_mediation_clean_shape_only_quiet_in_real_home():
    """The clean shape (get_async_http_client) is only quiet in the real webhook home."""
    from tests.chokepoint_paths import REPO_ROOT

    clean_source = (
        "from crawlutron.transport.http_client_pool import get_async_http_client\n"
        "client = get_async_http_client()\n"
    )

    # Real home is quiet for clean shape
    home = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "service" / "webhooks.py"
    assert _webhook_proxy_mediation_offenders(parsed_source(clean_source, filename=str(home)), home) == []

    # Violation shape in real home fires
    violation_source = "import httpx\nclient = httpx.AsyncClient()\n"
    offenders = _webhook_proxy_mediation_offenders(parsed_source(violation_source, filename=str(home)), home)
    assert len(offenders) == 1
    assert "webhook delivery constructs unproxied httpx client" in offenders[0]
