"""The proxy's dial marker is evidence only on plain-HTTP absolute-form responses."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping

import httpx
import pytest
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END

from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.runtime import process_state
from crawlutron.service import webhooks
from crawlutron.transport import egress_proxy as egress_mod
from crawlutron.transport import http_client_pool, pinned_dns, pooled_cffi, pooled_clients
from crawlutron.transport.cffi_transport import CffiTransport
from crawlutron.transport.egress_proxy import serve_proxy
from crawlutron.transport.transport import FetchRequest
from safe_url.host_policy import HostPolicy
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._walk_primitives import (
    canonical_tree,
    discovered_files,
    enclosing_function,
    nodes_of,
    offender_at,
    parent_map,
    parsed_source,
)
from tests.chokepoint_paths import REPO_ROOT, TRANSPORT_DIR, relative_posix

_MARKER = "x-crawlutron-egress-refusal"
_FAKE = "dial-failed"
_PUBLIC_IP = "93.184.216.34"
_LOGICAL = "logical.example"


def _mock_http_client(monkeypatch, answer) -> None:
    """Build the real pooled facade over an httpx MockTransport answer."""
    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(answer))

    monkeypatch.setattr(pooled_clients.httpx, "AsyncClient", factory)


def _cffi_response(headers: Mapping[str, str], body: bytes = b"origin-cffi"):
    """A curl_cffi-shaped streamed response answering one terminal body."""

    class _Response:
        status_code = 200

        def __init__(self) -> None:
            self.headers = headers
            self.queue: asyncio.Queue[object] = asyncio.Queue()
            self.queue.put_nowait(body)
            self.queue.put_nowait(STREAM_END)

        async def aclose(self) -> None:
            return None

    return _Response()


def _mock_cffi_session(monkeypatch, response) -> None:
    class _Session:
        async def get(self, *args: object, **kwargs: object):
            return response

    monkeypatch.setattr(http_client_pool, "get_cffi_session", lambda: _Session())


async def test_https_origin_marker_is_yielded_by_pooled_httpx(monkeypatch) -> None:
    """An HTTPS origin's marker is origin data, never a proxy ConnectError."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={_MARKER: _FAKE}, content=b"origin-httpx")

    _mock_http_client(monkeypatch, answer)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
        async with facade.stream("GET", request) as response:
            status = response.status_code
            body = await response.aread()
            marker = response.headers.get(_MARKER)
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    assert status == 200
    assert body == b"origin-httpx"
    assert marker == _FAKE
    assert len(seen) == 1


async def test_https_origin_marker_is_yielded_by_cffi(monkeypatch) -> None:
    """An HTTPS origin's marker must not trigger the httpx fallback."""
    _mock_cffi_session(monkeypatch, _cffi_response({_MARKER: _FAKE}))
    request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
    result = await CffiTransport().fetch(request, max_bytes=1024)
    assert result.status_code == 200
    assert result.body == b"origin-cffi"


async def test_https_webhook_marker_is_delivered_once(monkeypatch) -> None:
    """A hostile HTTPS receiver cannot turn one signed POST into retry storms."""
    posts: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, headers={_MARKER: _FAKE}, content=b"{}")

    _mock_http_client(monkeypatch, answer)
    monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "0")
    process_state.reset_all_process_state()
    try:
        await webhooks.post_webhook(
            WebhookTarget.admit("https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted()),
            {"results": []},
        )
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    assert len(posts) == 1, f"the signed payload was posted {len(posts)} times"


async def test_http_origin_marker_still_raises_connect_error(monkeypatch) -> None:
    """A plain-HTTP proxy refusal keeps its transport-error meaning."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, headers={_MARKER: _FAKE})

    _mock_http_client(monkeypatch, answer)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        request = FetchRequest(headers={}, url=canonical(f"http://{_LOGICAL}/page"), timeout_seconds=5.0)
        with pytest.raises(httpx.ConnectError, match="egress proxy could not reach the pinned target"):
            async with facade.stream("GET", request):
                pass
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()


async def test_cffi_http_origin_marker_still_raises_request_exception(monkeypatch) -> None:
    """The cffi family keeps falling back on a plain-HTTP proxy refusal."""
    response = _cffi_response({_MARKER: _FAKE})
    response.status_code = 502
    _mock_cffi_session(monkeypatch, response)
    request = FetchRequest(headers={}, url=canonical(f"http://{_LOGICAL}/page"), timeout_seconds=5.0)
    with pytest.raises(RequestException, match="egress proxy could not reach the pinned target"):
        await CffiTransport().fetch(request, max_bytes=1024)


async def test_https_connect_failure_stays_a_transport_error(monkeypatch) -> None:
    """A genuine HTTPS dial refusal is still a CONNECT failure, never a 200 page."""

    async def refuse_dial(host: str, port: int):
        raise OSError(f"unreachable {host}:{port}")

    monkeypatch.setattr(pinned_dns, "resolve_and_pin_host", lambda url: (url.host, _PUBLIC_IP))
    monkeypatch.setattr(egress_mod, "resolve_and_pin_host", lambda url: (url.host, _PUBLIC_IP))
    process_state.reset_all_process_state()
    proxy = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=refuse_dial)
    try:
        monkeypatch.setattr(
            pooled_cffi,
            "egress_proxy_url",
            lambda policy, settings=None: f"http://127.0.0.1:{proxy.port}",
        )
        request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
        with pytest.raises(RequestException):
            await CffiTransport().fetch(request, max_bytes=1024)
    finally:
        await proxy.aclose()
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()


# --- banned invariant: client marker classification has exactly one scheme-aware door ---

_DOOR_FILE = "packages/core/src/crawlutron/transport/transport.py"
_DOOR_METHOD = "is_proxy_dial_failure"


def _marker_names(tree: ast.Module) -> set[str]:
    """Module constants holding the marker, so indirection cannot dodge the gate."""
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if node.value.value != _MARKER:
            continue
        names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _is_marker(node: ast.AST, names: set[str]) -> bool:
    """True when an expression carries the marker directly or by module constant."""
    if isinstance(node, ast.Constant):
        return node.value == _MARKER
    return isinstance(node, ast.Name) and node.id in names


def _direct_marker_sites_offenders(tree: ast.Module, relative: str) -> list[str]:
    """Direct marker comparisons and header lookups outside the named door."""
    names = _marker_names(tree)
    parents = parent_map(tree)
    found: list[str] = []
    for node in nodes_of(tree):
        if isinstance(node, ast.Call):
            used = [_is_marker(arg, names) for arg in node.args]
            used.extend(_is_marker(kw.value, names) for kw in node.keywords)
        elif isinstance(node, ast.Compare):
            used = [_is_marker(node.left, names)]
            used.extend(_is_marker(c, names) for c in node.comparators)
        elif isinstance(node, ast.Subscript):
            used = [_is_marker(node.slice, names)]
        else:
            continue
        if not any(used):
            continue
        owner = enclosing_function(node, parents)
        if relative == _DOOR_FILE and owner is not None and owner.name == _DOOR_METHOD:
            continue
        found.append(offender_at(REPO_ROOT / relative, node, "scheme-blind marker lookup"))
    return found


def _synthetic_client(method: str, relative: str) -> ast.Module:
    """A synthetic client method with a direct marker lookup, for admission decoys."""
    source = (
        f'class FetchRequest:\n    def {method}(self, headers):\n        return headers.get("{_MARKER}")\n'
    )
    return parsed_source(source, filename=relative)


def test_no_scheme_blind_marker_check_outside_the_door() -> None:
    """Every client marker classification funnels through FetchRequest.is_proxy_dial_failure."""
    sources = discovered_files(TRANSPORT_DIR, "*.py")
    assert sources, f"empty transport scan root: {TRANSPORT_DIR}"
    found = [
        offense
        for path in sources
        for offense in _direct_marker_sites_offenders(canonical_tree(path), relative_posix(path))
    ]
    assert found == [], "scheme-blind marker checks must use the door:\n" + "\n".join(found)


def test_the_door_path_is_admitted_but_an_identically_named_sibling_is_refused() -> None:
    """Admission is by exact relative path, never by the door file's basename."""
    tree = _synthetic_client(_DOOR_METHOD, _DOOR_FILE)
    assert _direct_marker_sites_offenders(tree, _DOOR_FILE) == []
    sibling = "packages/core/src/crawlutron/transport/sibling/transport.py"
    found = _direct_marker_sites_offenders(tree, sibling)
    assert found, "an identically named sibling path inherited the door admission"
    assert "transport/sibling/transport.py:3" in found[0], found


def test_the_door_path_rejects_a_sibling_method() -> None:
    """The door method name is load-bearing; a scheme-blind sibling is refused."""
    tree = _synthetic_client("classify_without_scheme", _DOOR_FILE)
    found = _direct_marker_sites_offenders(tree, _DOOR_FILE)
    assert found, "a scheme-blind method at the door path was admitted"
    assert "transport.py:3" in found[0], found


def test_a_marker_constant_lookup_is_a_direct_check() -> None:
    """Indirection through the proxy's constant does not hide a client-side check."""
    relative = "packages/core/src/crawlutron/transport/client.py"
    source = (
        f'_REFUSAL_HEADER = "{_MARKER}"\n\ndef classify(headers):\n    return headers.get(_REFUSAL_HEADER)\n'
    )
    tree = parsed_source(source, filename=relative)
    found = _direct_marker_sites_offenders(tree, relative)
    assert found, "a marker lookup through a module constant escaped the gate"
    assert "client.py:4" in found[0], found
