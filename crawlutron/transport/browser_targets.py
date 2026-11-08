"""Browser targets: SSRF-gated navigation, route handlers and XHR capture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING, Protocol, cast

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.intercepted_xhr_record import InterceptedXhrRecord
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.json_safe import json_default
from crawlutron.transport.pinned_dns import (
    bounded_thread_resolve,
    check_ssrf_protection_stable,
)
from crawlutron.transport.pinned_transport import PinnedRequest

if TYPE_CHECKING:
    from crawlutron.models.json_value import JsonValue

# The resolve bound is the pinned transport's own: one implementation for both families.
_BOUNDED = bounded_thread_resolve


class RouteResponse(Protocol):
    """The XHR response surface the capture handler reads; a double needs exactly this."""

    url: str
    status: int
    headers: dict[str, str]

    async def json(self) -> object: ...


async def resolve_browser_target(
    url: NormalizedUrl,
    *,
    pin: Callable[..., Awaitable[PinnedRequest]],
    timeout_seconds: float | None = None,
) -> tuple[str, dict[str, str]]:
    """SSRF-check ``url`` for browser navigation, per scheme (#617).

    HTTPS keeps the hostname so TLS SNI and cert verification attest the pinned
    identity (an IP literal with a Host override never checks the cert) -- gated
    by the fresh+stable double-resolve the route and websocket guards share.
    Plain HTTP pins the resolved IP with the original Host: no TLS to verify.
    Unsafe targets raise. Async because pinning resolves DNS.
    """
    if url.scheme == "https":
        await _BOUNDED(
            lambda: check_ssrf_protection_stable(url),
            timeout_seconds=timeout_seconds,
        )
        return url.raw, {}
    pinned = await pin(url, {}, timeout_seconds=timeout_seconds)
    host = pinned.headers.get("Host")
    return pinned.connect_url, ({"Host": host} if host else {})


# Subresource types with no crawlable text: aborted in reduced-resource mode.
# Tracking/analytics hosts are never fetched (NormalizedUrl.is_analytics_host, shared with enqueue refusal).
_REDUCED_BLOCK_RESOURCE_TYPES = frozenset({"image", "font", "media"})


def make_ssrf_route_handler(
    resource_mode: str | None = None,
    *,
    include_screenshot: bool = False,
    policy: HostPolicy,
    pin: Callable[..., PinnedRequest],
    timeout_seconds: float | None = None,
):
    """A Playwright ``page.route`` handler aborting requests to SSRF-unsafe hosts:
    every navigation, redirect, and subresource is blocklist-checked (DNS in a
    thread pool). Plain HTTP uses the fresh pinned IP; HTTPS keeps the hostname.
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
        # Analytics and presentation subresources never consume crawl bandwidth:
        # drop unconditionally, or when reduced-mode says so and no screenshot.
        if admitted.is_analytics_host or (
            getattr(request, "resource_type", "") in _REDUCED_BLOCK_RESOURCE_TYPES and not include_screenshot
        ):
            await route.abort()
            return
        resolution = await _resolve_ssrf_pinned_url(
            request,
            request_scheme,
            admitted,
            pin=pin,
            timeout_seconds=timeout_seconds,
            abort=route.abort,
        )
        if resolution is None:
            return  # the failure path already aborted (fail closed)
        pinned_url, host_headers = resolution
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


async def _resolve_ssrf_pinned_url(
    request,
    request_scheme: str,
    admitted: NormalizedUrl,
    *,
    pin: Callable[..., PinnedRequest],
    timeout_seconds: float | None,
    abort: Callable[[], Awaitable[None]],
) -> tuple[str, dict[str, str]] | None:
    """Pin the request URL per scheme; None after aborting (fail closed).

    HTTPS validates fresh and stable; plain HTTP re-pins through
    the caller's rule.
    """
    try:
        if request_scheme == "https":
            # Two fresh resolves must agree: the browser dials after this check
            # with no dial ownership, so a changed answer is the rebinding signature.
            await _BOUNDED(
                lambda: check_ssrf_protection_stable(admitted),
                timeout_seconds=timeout_seconds,
            )
            return request.url, {}
        pinned = await _BOUNDED(lambda: pin(admitted, {}), timeout_seconds=timeout_seconds)
        return pinned.connect_url, pinned.headers
    except Exception:
        # Fail closed: DNS timeout, resolver errors, anything unexpected ->
        # abort rather than let an unchecked connection proceed.
        await abort()
        return None


def make_ssrf_websocket_handler(*, policy: HostPolicy, timeout_seconds: float | None = None):
    """A Playwright websocket-route handler blocking SSRF-unsafe WS targets:
    ``page.route`` cannot intercept WebSocket upgrades, so this closes any that
    resolve internal and forwards the rest (``connect_to_server``).
    """

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
            normalized = await _BOUNDED(
                lambda: NormalizedUrl(http_equivalent, policy=policy), timeout_seconds=timeout_seconds
            )
            # Fresh and stable, never TTL-cached; a changed answer between
            # immediate repeats is the rebinding signature.
            await _BOUNDED(
                lambda: check_ssrf_protection_stable(normalized),
                timeout_seconds=timeout_seconds,
            )
        except Exception:
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
