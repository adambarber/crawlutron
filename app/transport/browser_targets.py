"""Browser targets: SSRF-gated navigation, route handlers and XHR capture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any

from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.safety.json_safe import json_default
from app.transport.pinned_transport import check_ssrf_protection
from app.vocabulary.hosts import is_analytics_host as _is_analytics_url


async def resolve_browser_target(
    url: NormalizedUrl,
    *,
    pin: Callable[..., Awaitable[tuple[str, dict[str, str]]]],
) -> tuple[str, dict[str, str]]:
    """SSRF-check + IP-pin ``url`` for browser navigation.

    Returns ``(pinned_url, extra_http_headers)``. The browser is told to connect
    to the single resolved IP (preventing DNS-rebinding) while preserving the
    original ``Host`` header. Raises ``ValueError`` if the target is unsafe --
    the value's own policy decides whether the target is reachable at all.

    Async because pinning resolves DNS, and a render is not the only thing on
    the loop: called synchronously this ran ``socket.getaddrinfo`` inline, so every
    other in-flight crawl waited out the resolver once per rendered page.

    ``pin`` is a required argument, not a keyword with a default. It is the rule
    that decides whether this crawl may reach the URL at all, so a caller that
    forgot to pass one would silently fall back to a weaker rule; the type is
    declared here rather than imported so the dependency is visible at the call.
    """
    pinned_url, request_headers = await pin(url, {})
    host = request_headers.get("Host")
    return pinned_url, ({"Host": host} if host else {})


# Subresource types with no crawlable text: aborted in reduced-resource mode.
# Tracking/analytics hosts are never fetched (app.vocabulary.hosts, shared with enqueue refusal).
_REDUCED_BLOCK_RESOURCE_TYPES = frozenset({"image", "font", "media"})


def make_ssrf_route_handler(
    resource_mode: str | None = None,
    *,
    include_screenshot: bool = False,
    policy: HostPolicy,
    pin: Callable[..., tuple[str, dict[str, str]]],
):
    """Playwright route handler that aborts requests to SSRF-unsafe hosts.

    Registered via ``page.route`` so the main navigation, server-side
    redirects, and every subresource load (images/scripts/XHR) is checked
    against the SSRF blocklist. Requests that resolve to internal/loopback/
    reserved addresses are aborted before the browser connects.

    The SSRF check involves synchronous DNS resolution (socket.getaddrinfo).
    To avoid blocking the event loop, the check runs in a thread pool via
    ``asyncio.to_thread``.

    Plain-HTTP requests keep the FRESH (uncached) pinned IP, which governs the
    actual connection: a rebind inside the resolver cache TTL must not slip
    onto the wire. HTTPS requests are validated through the
    same cached check the websocket handler uses: ``build_pinned_request``
    keeps the original hostname there (real certificates carry no IP address
    SAN, so a pin would only break SNI/cert verification), making an uncached
    pin resolve per subresource pure cost with no security gain. HTTPS stays
    blocklist-checked before the browser connects.
    """

    async def _handler(route, request):
        # Re-pin the IP per subresource request (DNS-rebinding TOCTOU): raises on
        # private/loopback, returns (pinned_url, {Host: hostname}) to preserve routing.
        request_scheme = request.url.split(":", 1)[0].lower()
        if request_scheme not in ("http", "https", ""):
            # Data:, blob:, about:, javascript: never touch the network via DNS;
            # let the browser handle them locally instead of aborting.
            await route.continue_()
            return
        # The browser-observed URL is a live-text boundary: admitted here, once,
        # inline; an unadmittable URL reaches the same "unsafe target" abort below.
        try:
            admitted = NormalizedUrl(request.url, policy=policy)
        except ValueError:
            await route.abort()
            return
        # Tracking/analytics requests carry no crawlable content; drop them
        # unconditionally so they never consume bandwidth or trigger SSRF resolution.
        if _is_analytics_url(admitted):
            await route.abort()
            return
        # Abort presentation subresources: a text crawler never needs them (~70%
        # of render bandwidth); permitted only when a screenshot is requested.
        if getattr(request, "resource_type", "") in _REDUCED_BLOCK_RESOURCE_TYPES and not include_screenshot:
            await route.abort()
            return
        try:
            # Annotated once for both branches: mypy cannot widen the https
            # branch's empty literal to the dict type the http pin rule returns.
            host_headers: dict[str, str]
            if request_scheme == "https":
                # https discards the pinned IP (TLS SNI/certs need the hostname), so
                # validate via the cached TTL'd check: one resolve per host, fail-closed.
                await asyncio.to_thread(check_ssrf_protection, admitted)
                pinned_url, host_headers = request.url, {}
            else:
                pinned_url, host_headers = await asyncio.to_thread(
                    pin,
                    admitted,
                    {},
                )
        except Exception:
            # Fail closed: DNS timeout, resolver errors, anything unexpected ->
            # abort rather than let an unchecked connection proceed.
            await route.abort()
            return
        overrides = {}
        if host_headers.get("Host"):
            overrides["Host"] = host_headers["Host"]
        # Omit headers= when empty: Playwright treats an explicit dict as a full
        # replacement, so {} would wipe UA/cookies/accept off every HTTPS subresource.
        if overrides:
            await route.continue_(url=pinned_url, headers=overrides)
        else:
            await route.continue_(url=pinned_url)

    return _handler


def make_ssrf_websocket_handler(*, policy: HostPolicy):
    """Playwright websocket-route handler blocking SSRF-unsafe WS targets.

    page.route does not intercept WebSocket upgrades; without this a hostile
    page could open a WebSocket to an internal host around the route guard.
    Validation reuses check_ssrf_protection with ws/wss mapped to http/https.
    """

    async def _ws_handler(ws):
        url = getattr(ws, "url", "") or ""
        scheme = url.split(":", 1)[0].lower()
        if scheme not in ("ws", "wss"):
            return  # not a websocket target
        http_equivalent = ("https" if scheme == "wss" else "http") + url[len(scheme) :]
        try:
            normalized = await asyncio.to_thread(
                NormalizedUrl,
                http_equivalent,
                policy=policy,
            )
            await asyncio.to_thread(check_ssrf_protection, normalized)
        except Exception:
            close = getattr(ws, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    return _ws_handler


def make_response_handler(collected: list[dict[str, Any]]) -> Callable[[Any], Awaitable[None]]:
    """Collect bounded JSON XHR payloads into the shared list."""
    # XHR bounds: 10 entries x 8KB payload; URLs are page-controlled too.
    max_entries = 10
    max_payload_chars = 8192
    max_url_chars = 2048

    async def on_response(response: Any) -> None:
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
        url = str(getattr(response, "url", ""))
        entry: dict[str, Any] = {
            "url": url[:max_url_chars],
            "content_type": content_type,
            "status": getattr(response, "status", None),
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
            entry["payload"] = payload
        collected.append(entry)

    return on_response
