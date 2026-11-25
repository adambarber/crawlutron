"""Browser targets: SSRF-gated navigation, route handlers and XHR capture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, cast

from crawlutron.models.intercepted_xhr_record import InterceptedXhrRecord
from crawlutron.safety.json_safe import json_default
from crawlutron.transport.pinned_transport import PinnedRequest
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

if TYPE_CHECKING:
    from crawlutron.models.json_value import JsonValue


class RouteResponse(Protocol):
    """The XHR response surface the capture handler reads; a double needs exactly this."""

    url: str
    status: int
    headers: dict[str, str]

    async def json(self) -> object: ...


async def resolve_browser_target(
    url: NormalizedUrl,
    *,
    pin: Callable[..., Awaitable[PinnedRequest]] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[str, dict[str, str]]:
    """Return logical URL for proxy-mediated navigation; proxy owns dialing (#991)."""
    # S4: browser navigates logical hostname; proxy validates and dials.
    return url.raw, {}


# Subresource types with no crawlable text: aborted in reduced-resource mode.
# Tracking/analytics hosts are never fetched (NormalizedUrl.is_analytics_host, shared with enqueue refusal).
_REDUCED_BLOCK_RESOURCE_TYPES = frozenset({"image", "font", "media"})

# Local-harmless subresource schemes: no DNS, delegated to the browser. Every
# other non-http(s) scheme (file:, ftp:, ws:, chrome:, unknown) is refused.
_LOCAL_HARMLESS_SCHEMES = frozenset({"data", "blob", "about", "javascript"})


def make_ssrf_route_handler(
    resource_mode: str | None = None,
    *,
    include_screenshot: bool = False,
    policy: HostPolicy,
    pin: Callable[..., Awaitable[PinnedRequest]] | None = None,
    timeout_seconds: float | None = None,
):
    """Route handler: NormalizedUrl admit/abort, continue logical URL via proxy."""

    async def _handler(route, request):
        request_scheme = request.url.split(":", 1)[0].lower()
        if request_scheme in _LOCAL_HARMLESS_SCHEMES:
            await route.continue_()
            return
        if request_scheme not in ("http", "https", ""):
            await route.abort()
            return
        # The browser-observed URL is a live-text boundary: admitted here, once,
        # inline; an unadmittable URL reaches the same "unsafe target" abort below.
        try:
            admitted = NormalizedUrl(request.url, policy=policy)
        except ValueError:
            await route.abort()
            return
        # Analytics and presentation subresources never consume crawl bandwidth:
        # drop unconditionally, or when reduced-mode says so and no screenshot.
        if admitted.is_analytics_host or (
            getattr(request, "resource_type", "") in _REDUCED_BLOCK_RESOURCE_TYPES and not include_screenshot
        ):
            await route.abort()
            return
        # With loopback egress proxy, continue with logical URL.
        # DNS resolution happens on proxy side.
        await route.continue_(url=request.url)

    return _handler


def make_ssrf_websocket_handler(*, policy: HostPolicy, timeout_seconds: float | None = None):
    """WS route handler: NormalizedUrl admit/abort, forward via proxy."""

    async def _ws_handler(ws):
        url = getattr(ws, "url", "") or ""
        scheme = url.split(":", 1)[0].lower()
        if scheme not in ("ws", "wss"):
            # Not a websocket upgrade: hand back to the browser unchanged.
            connect = getattr(ws, "connect_to_server", None)
            if connect is not None:
                connect()
            return
        http_equivalent = ("https" if scheme == "wss" else "http") + url[len(scheme) :]
        try:
            # Normalize the URL through the policy; the proxy will resolve and dial.
            NormalizedUrl(http_equivalent, policy=policy)
        except ValueError:
            close = getattr(ws, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        else:
            # An allowed target must actually forward to keep the socket working:
            # before, the handler dead-ended every allowed connection.
            connect = getattr(ws, "connect_to_server", None)
            if connect is not None:
                connect()

    return _ws_handler


def make_response_handler(
    collected: list[InterceptedXhrRecord],
) -> Callable[[RouteResponse], Awaitable[None]]:
    """Collect bounded JSON XHR payloads into the shared list."""
    # XHR bounds: 10 entries x 8KB payload; URLs are page-controlled too.
    max_entries = 10
    max_payload_chars = 8192
    max_url_chars = 2048

    async def on_response(response: RouteResponse) -> None:
        if len(collected) >= max_entries:
            return  # hostile pages can fire thousands of JSON responses
        headers = response.headers or {}
        content_type = ""
        if isinstance(headers, dict):
            for key, value in headers.items():
                if str(key).lower() == "content-type":
                    content_type = str(value)
                    break
        if "application/json" not in content_type:
            return
        url = response.url
        entry: InterceptedXhrRecord = {
            "url": url[:max_url_chars],
            "content_type": content_type,
            "status": response.status,
            "payload": None,
        }
        if len(url) > max_url_chars:
            entry["url_truncated"] = True
        try:
            # Residual: the browser materialized this body, so the cap applies after one parse.
            payload = await response.json()
        except Exception:  # payload capture is best-effort
            payload = None
        if payload is not None and len(json.dumps(payload, default=json_default)) > max_payload_chars:
            entry["payload_truncated"] = True
        else:
            # Parsed JSON is a JsonValue by construction; the record says so.
            entry["payload"] = cast("JsonValue", payload)
        collected.append(entry)

    return on_response


# Compat alias: S4 renamed to make_ssrf_route_handler.
ssrf_route_handler = make_ssrf_route_handler
