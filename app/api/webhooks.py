"""Webhook delivery helpers (SSRF-safe, IP-pinned): every outbound callback routes
through ``post_webhook`` -> ``deliver_pinned_webhook``, re-admitting and re-pinning
via the fetchers' one rule so validation-to-delivery TOCTOU stays closed; nothing else composes deliver.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx

from app.config.env import get_mapping_int
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.safety.bounded_io import BodyTooLarge, read_capped_async
from app.transport.timeouts import Timeouts

logger = logging.getLogger(__name__)


async def deliver_pinned_webhook(
    webhook_url: str,
    payload_bytes: bytes,
    *,
    host_policy: HostPolicy,
) -> None:
    """POST ``payload_bytes`` to ``webhook_url`` with DNS-rebinding safety.
    No URL composition lives here -- build_pinned_request owns the pin, so
    IPv6/Host handling cannot drift; the caller MUST have validated the URL first.
    """
    # one place) rather than urlparse/check/pin inline -- manual composition is the
    # shape that produced the redirect findings, and this closes validation's window.
    from app.transport.pinned_transport import build_pinned_request_async

    connect_url, connect_headers = await build_pinned_request_async(
        NormalizedUrl(webhook_url, policy=host_policy), {}
    )
    request_headers = {"Content-Type": "application/json", **connect_headers}
    # Deliberate one-per-delivery cost: pooling would re-resolve by hostname (defeating
    # the pin) or cache the IP across deliveries (reopening the rebinding window).
    async with httpx.AsyncClient(timeout=Timeouts.defaults().as_httpx()) as client:
        async with client.stream(
            "POST", connect_url, content=payload_bytes, headers=request_headers
        ) as response:
            try:
                # The webhook RESPONSE is attacker-controlled: drain it through
                # the bounded_io cap instead of buffering it in RAM.
                await read_capped_async(response.aiter_bytes(), max_webhook_size())
            except BodyTooLarge:
                logger.warning(
                    "webhook_response_too_large",
                    extra={"limit_bytes": max_webhook_size()},
                )


_WEBHOOK_SIZE_DEFAULT = 10_485_760


@functools.lru_cache(maxsize=256)
def _parse_webhook_size(raw: str | None) -> int:
    """Parse one distinct raw env value into the bounded webhook cap.
    The parse and its never-raise ``lo=1`` contract live in get_mapping_int;
    caching on the raw string parses once per value yet honors an override next call.
    """
    mapping = {} if raw is None else {"CRAWLUTRON_MAX_WEBHOOK_SIZE": raw}
    return get_mapping_int(mapping, "CRAWLUTRON_MAX_WEBHOOK_SIZE", _WEBHOOK_SIZE_DEFAULT, lo=1)


def max_webhook_size() -> int:
    """The outbound webhook payload/response size cap.
    Cached on the raw env value so the parse runs once per distinct value,
    and an override (operator env or a test) is honored on the very next call.
    """
    return _parse_webhook_size(os.environ.get("CRAWLUTRON_MAX_WEBHOOK_SIZE"))


async def post_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    host_policy: HostPolicy,
) -> None:
    """POST a webhook payload with size limit and SSRF+IP pinning, via the same
    validated delivery path (no TOCTOU window); size-capping and best-effort
    failure handling live here so a dead callback never crashes the crawl worker.
    """
    payload_bytes = json.dumps(dict(payload)).encode("utf-8")
    if len(payload_bytes) > max_webhook_size():
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
    """Validate a webhook URL against SSRF: resolves the hostname and rejects any
    that maps to a private/loopback address (DNS-rebinding class), implemented
    on the one pin rule (admission + resolve-and-pin); raises ValueError if unsafe.
    """
    from app.transport.pinned_transport import build_pinned_request

    build_pinned_request(NormalizedUrl(webhook_url, policy=host_policy), {})


async def validate_webhook_url_async(webhook_url: str, *, host_policy: HostPolicy) -> None:
    """Off-event-loop twin of :func:`validate_webhook_url`: the sync getaddrinfo
    would stall the loop on a slow resolver, so this reuses the fetch path's
    dedicated dns pool; every webhook caller is async, so they route here.
    """
    from app.transport.pinned_transport import build_pinned_request_async

    await build_pinned_request_async(NormalizedUrl(webhook_url, policy=host_policy), {})


__all__ = [
    "deliver_pinned_webhook",
    "max_webhook_size",
    "post_webhook",
    "validate_webhook_url",
    "validate_webhook_url_async",
]
