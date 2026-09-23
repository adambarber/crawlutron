"""Webhook delivery helpers (SSRF-safe + IP-pinned).

All outbound webhook callbacks route through this module so the SSRF guard,
DNS-pinning, and payload-size cap are applied uniformly. The original
implementation lived inline in ``app/main.py``; splitting it out makes the
endpoint handlers easier to read and lets integration tests target the
helpers directly.

The one delivery funnel is :func:`post_webhook`: it validates, size-caps,
and delegates to :func:`deliver_pinned_webhook`, which re-parses through
``SafeUrl`` to close the TOCTOU between validation and delivery. Nothing else
in ``app/`` may POST to a webhook URL or compose a crawl with a delivery --
the crawl-plus-deliver story lives solely in
:mod:`app.api.async_crawl_runner`, and the response payload is always built by
:func:`app.api.response_shaping.build_unified_crawl_response` — the one shaping
chokepoint, called directly (never a swappable hook, so a test cannot
reinstiate a drift-prone construction path).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from app.bounded_io import BodyTooLarge, read_capped_async
from app.timeouts import Timeouts
from app.validation import HostPolicy, get_mapping_int

if TYPE_CHECKING:
    from app.safe_url import SafeUrl

logger = logging.getLogger(__name__)


async def deliver_pinned_webhook(
    webhook_url: str,
    payload_bytes: bytes,
    *,
    host_policy: HostPolicy,
) -> None:
    """POST ``payload_bytes`` to ``webhook_url`` with DNS-rebinding safety.

    The pin policy lives in ``SafeUrl.connect()`` (plain HTTP connects to the
    validated IP with the original ``Host`` header; HTTPS keeps the hostname
    because certs carry no IP SANs -- the residual rebinding window there is
    the same posture the fetcher accepts: pre-connect check + CA validation).
    This helper holds no URL composition of its own, so the IPv6 bracketing
    and Host handling cannot drift from the fetchers (H5-C).

    Caller MUST have validated ``webhook_url`` via ``validate_webhook_url``
    first; this helper assumes the URL has already passed the SSRF guard.

    Consolidated from ``run_and_deliver_webhook`` so the ``post_webhook``
    path shares the same TOCTOU-safe delivery code (adversarial review
    F-webhook-toctou).
    """
    import httpx

    # Re-parse through the chokepoint (normalize + check + pin in one place)
    # rather than reimplementing urlparse/check/pin inline -- the manual
    # composition is exactly the shape that produced the redirect findings.
    # validate_webhook_url already ran at the edge; parsing again here closes
    # the TOCTOU between validation and delivery. The parse runs off the event
    # loop (D3): see _parse_pinned_off_loop.
    safe = await _parse_pinned_off_loop(webhook_url, host_policy=host_policy)
    connect_url, connect_headers = safe.connect()
    request_headers = {"Content-Type": "application/json", **connect_headers}
    # A fresh client per delivery is deliberate (M4 audit): the security
    # posture REQUIRES a per-delivery parse+resolve+pin (line above; the
    # F-webhook-toctou consolidation), and pooling connections across
    # deliveries would either re-resolve by hostname (defeating the pin) or
    # cache the IP across deliveries (reopening the rebinding window). The
    # handshake cost is one-per-job-completion, and a pooled client would
    # also inherit the aclose lifecycle discipline the crawl side had to
    # build for close races. Not a duplication finding.
    async with httpx.AsyncClient(timeout=Timeouts.defaults().as_httpx()) as client:
        async with client.stream(
            "POST", connect_url, content=payload_bytes, headers=request_headers
        ) as response:
            try:
                # The webhook RESPONSE is attacker-controlled: drain it through
                # the bounded_io cap instead of buffering it in RAM (M3).
                await read_capped_async(response.aiter_bytes(), _max_webhook_size())
            except BodyTooLarge:
                logger.warning(
                    "webhook_response_too_large",
                    extra={"limit_bytes": _max_webhook_size()},
                )


_WEBHOOK_SIZE_DEFAULT = 10_485_760


@functools.lru_cache(maxsize=256)
def _parse_webhook_size(raw: str | None) -> int:
    """Parse one distinct raw env value into the bounded webhook cap (P2-F).

    The parse -- and its never-raise contract plus the ``lo=1`` floor -- stays
    in ``get_mapping_int``; caching on the raw string means the parse runs once
    per distinct value while an override is still picked up on the next call.
    """
    mapping = {} if raw is None else {"BROWSER_CRAWLER_MAX_WEBHOOK_SIZE": raw}
    return get_mapping_int(mapping, "BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", _WEBHOOK_SIZE_DEFAULT, lo=1)


def _max_webhook_size() -> int:
    """The outbound webhook payload/response size cap.

    Read 2-3 times per delivery, this used to re-read and re-parse
    ``os.environ`` on every call (P2-F). It is cached on the raw env value, so
    the parse runs once per distinct value; the key is the value itself, so an
    override -- operator env, or a test -- is honored on the very next call
    instead of being shadowed by a stale cached parse.
    """
    return _parse_webhook_size(os.environ.get("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE"))


async def post_webhook(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    host_policy: HostPolicy,
) -> None:
    """POST payload to webhook URL with size limit and SSRF+IP-pinning.

    Routes through the same validated + IP-pinned delivery path
    (:func:`deliver_pinned_webhook`) so a malicious or rebinding webhook URL cannot
    escape the SSRF guard between endpoint validation and delivery
    (TOCTOU window). The size cap and best-effort failure handling live
    here so the crawl worker is never crashed by a dead callback.
    """
    payload_bytes = json.dumps(payload).encode("utf-8")
    if len(payload_bytes) > _max_webhook_size():
        logger.warning(
            "webhook_payload_too_large",
            extra={"url": webhook_url, "size": len(payload_bytes)},
        )
        return
    try:
        await validate_webhook_url_async(webhook_url, host_policy=host_policy)
        await deliver_pinned_webhook(
            webhook_url,
            payload_bytes,
            host_policy=host_policy,
        )
    except Exception as exc:
        logger.warning(
            "webhook_delivery_failed",
            extra={"url": webhook_url, "error": str(exc)},
        )


def validate_webhook_url(webhook_url: str, *, host_policy: HostPolicy) -> None:
    """Validate webhook URL to prevent SSRF attacks.

    Similar to crawl target validation but for outbound webhook callbacks.
    Resolves the hostname and rejects any that map to a private/loopback
    address, protecting against attacker-controlled DNS that returns an
    internal IP (DNS-rebinding class of SSRF).

    Implemented on the SafeUrl chokepoint: normalize (scheme/hostname/
    private-name rules) + DNS check in one place. Raises ValueError if
    unsafe; returns None when the URL is safe to deliver to.
    """
    from app.safe_url import SafeUrl

    SafeUrl.parse(webhook_url, policy=host_policy, pin=True)


async def validate_webhook_url_async(webhook_url: str, *, host_policy: HostPolicy) -> None:
    """Off-event-loop twin of :func:`validate_webhook_url` (M6).

    ``SafeUrl.parse(pin=True)`` calls the synchronous ``socket.getaddrinfo``,
    so running validation inline stalls the event loop whenever a
    caller-chosen webhook hostname has a slow or blackholed resolver. Crawl
    targets already offload the identical resolution off-thread via
    ``_build_pinned_request_async``; this reuses that same dedicated
    ``dns-resolve`` pool rather than inventing a second offload mechanism.
    Every caller of ``validate_webhook_url`` is already async, so they route
    here instead.
    """
    from app.crawl.strategies.http import _DNS_EXECUTOR

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _DNS_EXECUTOR,
        functools.partial(validate_webhook_url, webhook_url, host_policy=host_policy),
    )


async def _parse_pinned_off_loop(webhook_url: str, *, host_policy: HostPolicy) -> SafeUrl:
    """Off-event-loop ``SafeUrl.parse(webhook_url, pin=True)`` (D3).

    The delivery path re-parses through the chokepoint to close the TOCTOU
    between validation and delivery, and that parse is the SAME synchronous
    ``getaddrinfo`` the validation twin offloads in M6. Running it inline would
    stall the whole FastAPI loop on every webhook delivery -- the validation
    path was offloaded and this one was missed -- so it reuses the ONE
    ``dns-resolve`` pool rather than a second offload mechanism. Both the
    validation and the delivery parse route through here or through
    :func:`validate_webhook_url_async`; there is no third resolution path.
    """
    import functools

    from app.crawl.strategies.http import _DNS_EXECUTOR
    from app.safe_url import SafeUrl

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DNS_EXECUTOR,
        functools.partial(SafeUrl.parse, webhook_url, policy=host_policy, pin=True),
    )


__all__ = [
    "deliver_pinned_webhook",
    "post_webhook",
    "validate_webhook_url",
    "validate_webhook_url_async",
]
