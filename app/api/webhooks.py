"""Webhook delivery helpers (SSRF-safe + IP-pinned).

All outbound webhook callbacks route through this module so the SSRF guard,
DNS-pinning, and payload-size cap are applied uniformly. The original
implementation lived inline in ``app/main.py``; splitting it out makes the
endpoint handlers easier to read and lets integration tests target the
helpers directly.

Module-level ``set_webhook_dependencies`` wires the app context
(``webhook_semaphore``, ``shutdown_event``, ``crawl_request``,
``pool_getter``, ``get_metrics``) once at startup so the delivery helpers
can stay stateless and the tests can mock individual hooks instead of
constructing full FastAPI dependency trees. The response payload itself is
always built by :func:`app.api.response_shaping.build_unified_crawl_response`
— the one shaping chokepoint, called directly (never a swappable hook, so a
test cannot reinstate a drift-prone construction path).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.api.response_shaping import build_unified_crawl_response
from app.bounded_io import BodyTooLarge, read_capped_async
from app.settings import CrawlerSettings
from app.timeouts import Timeouts
from app.validation import get_env_int

logger = logging.getLogger(__name__)


# Module-level app context. Wired once at FastAPI lifespan startup via
# :func:`set_webhook_dependencies`. Cleared on teardown so unit tests start
# from a known state.
_webhook_semaphore: asyncio.Semaphore | None = None
_shutdown_event: asyncio.Event | None = None
_crawl_request_fn = None
_pool_getter = None
_get_metrics_fn = None


def set_webhook_dependencies(
    *,
    webhook_semaphore: asyncio.Semaphore,
    shutdown_event: asyncio.Event,
    crawl_request_fn,
    pool_getter,
    get_metrics,
) -> None:
    """Install app context for the webhook helpers (called from lifespan)."""
    global _webhook_semaphore, _shutdown_event, _crawl_request_fn, _pool_getter
    global _get_metrics_fn
    _webhook_semaphore = webhook_semaphore
    _shutdown_event = shutdown_event
    _crawl_request_fn = crawl_request_fn
    _pool_getter = pool_getter
    _get_metrics_fn = get_metrics


def clear_webhook_dependencies() -> None:
    """Reset module-level state (used by lifespan teardown and tests)."""
    global _webhook_semaphore, _shutdown_event, _crawl_request_fn, _pool_getter
    global _get_metrics_fn
    _webhook_semaphore = None
    _shutdown_event = None
    _crawl_request_fn = None
    _pool_getter = None
    _get_metrics_fn = None


async def deliver_pinned_webhook(webhook_url: str, payload_bytes: bytes) -> None:
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

    from app.safe_url import SafeUrl

    # Re-parse through the chokepoint (normalize + check + pin in one
    # place) rather than reimplementing urlparse/check/pin inline -- the
    # manual composition is exactly the shape that produced the redirect
    # findings. validate_webhook_url already ran at the edge; parsing again
    # here closes the TOCTOU between validation and delivery.
    safe = SafeUrl.parse(webhook_url, pin=True)
    connect_url, connect_headers = safe.connect()
    request_headers = {"Content-Type": "application/json", **connect_headers}
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


def _max_webhook_size() -> int:
    return get_env_int("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", 10_485_760, lo=1)


async def post_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    """POST payload to webhook URL with size limit and SSRF+IP-pinning.

    Routes through the same validated + IP-pinned delivery path used by
    :func:`deliver_webhook` so a malicious or rebinding webhook URL cannot
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
        validate_webhook_url(webhook_url)
        await deliver_pinned_webhook(webhook_url, payload_bytes)
    except Exception as exc:
        logger.warning(
            "webhook_delivery_failed",
            extra={"url": webhook_url, "error": str(exc)},
        )


def validate_webhook_url(webhook_url: str) -> None:
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

    SafeUrl.parse(webhook_url, pin=True)


async def run_and_deliver_webhook(
    webhook_url: str,
    request_id: str,
    payload_dict: dict[str, Any],
    crawl_settings: CrawlerSettings,
) -> None:
    """Run a crawl in the background and POST the result to ``webhook_url``.

    Concurrency is capped by the module-level ``_webhook_semaphore`` (set
    by ``set_webhook_dependencies``); excess deliveries wait on the
    semaphore instead of multiplying resource usage. Delivery failures
    are swallowed so a dead callback never crashes the crawler worker.
    """
    from app.crawl.strategies.browser_pool import PooledBrowserWorker

    if any(
        x is None
        for x in (
            _webhook_semaphore,
            _shutdown_event,
            _crawl_request_fn,
            _pool_getter,
            _get_metrics_fn,
        )
    ):
        raise RuntimeError(
            "Webhook dependencies not initialized. Call set_webhook_dependencies() from the FastAPI lifespan."
        )
    validate_webhook_url(webhook_url)
    pooled_worker = PooledBrowserWorker(_pool_getter())
    result = await _crawl_request_fn(
        payload_dict,
        settings=crawl_settings,
        browser_worker=pooled_worker,
        shutdown_event=_shutdown_event,
    )
    result["request_id"] = request_id
    _get_metrics_fn().observe_crawl(results=result.get("results", []))
    response_payload = build_unified_crawl_response(
        result.get("results", []),
        request_id=request_id,
        submitted_count=result.get("submitted_count"),
        policy_schema_version=result.get("policy_schema_version", ""),
    ).model_dump()
    try:
        payload_bytes = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
        if len(payload_bytes) > _max_webhook_size():
            logger.warning(
                "webhook_payload_too_large",
                extra={"size": len(payload_bytes), "limit": _max_webhook_size()},
            )
            return
        await deliver_pinned_webhook(webhook_url, payload_bytes)
    except Exception as error:
        logger.warning("webhook_delivery_failed: %s", error, extra={"webhook_url": webhook_url})


async def deliver_webhook(
    webhook_url: str,
    request_id: str,
    payload_dict: dict[str, Any],
    crawl_settings: CrawlerSettings,
) -> None:
    """Throttled webhook entry point: caps concurrent crawls, then runs + delivers.

    Best-effort: failures are swallowed so a dead callback never crashes
    the crawler worker. This is the public entry point used by the
    ``/v1/crawl`` background-task path.
    """
    if _webhook_semaphore is None:
        raise RuntimeError(
            "Webhook dependencies not initialized. Call set_webhook_dependencies() from the FastAPI lifespan."
        )
    async with _webhook_semaphore:
        await run_and_deliver_webhook(webhook_url, request_id, payload_dict, crawl_settings)


__all__ = [
    "set_webhook_dependencies",
    "clear_webhook_dependencies",
    "deliver_pinned_webhook",
    "post_webhook",
    "validate_webhook_url",
    "run_and_deliver_webhook",
    "deliver_webhook",
]
