"""Webhook delivery helpers (SSRF-safe + IP-pinned).

All outbound webhook callbacks route through this module so the SSRF guard,
DNS-pinning, and payload-size cap are applied uniformly. The original
implementation lived inline in ``app/main.py``; splitting it out makes the
endpoint handlers easier to read and lets integration tests target the
helpers directly.

Module-level ``set_webhook_dependencies`` wires the app context
(``webhook_semaphore``, ``shutdown_event``, ``crawl_request``,
``build_unified_result``, ``unified_response_cls``, ``pool_getter``,
``get_metrics``) once at startup so the delivery helpers can stay stateless
and the tests can mock individual hooks instead of constructing full
FastAPI dependency trees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


# Module-level app context. Wired once at FastAPI lifespan startup via
# :func:`set_webhook_dependencies`. Cleared on teardown so unit tests start
# from a known state.
_webhook_semaphore: asyncio.Semaphore | None = None
_shutdown_event: asyncio.Event | None = None
_crawl_request_fn = None
_pool_getter = None
_unified_response_cls = None
_build_unified_result_fn = None
_get_metrics_fn = None


def set_webhook_dependencies(
    *,
    webhook_semaphore: asyncio.Semaphore,
    shutdown_event: asyncio.Event,
    crawl_request_fn,
    pool_getter,
    unified_response_cls,
    build_unified_result,
    get_metrics,
) -> None:
    """Install app context for the webhook helpers (called from lifespan)."""
    global _webhook_semaphore, _shutdown_event, _crawl_request_fn, _pool_getter
    global _unified_response_cls, _build_unified_result_fn, _get_metrics_fn
    _webhook_semaphore = webhook_semaphore
    _shutdown_event = shutdown_event
    _crawl_request_fn = crawl_request_fn
    _pool_getter = pool_getter
    _unified_response_cls = unified_response_cls
    _build_unified_result_fn = build_unified_result
    _get_metrics_fn = get_metrics


def clear_webhook_dependencies() -> None:
    """Reset module-level state (used by lifespan teardown and tests)."""
    global _webhook_semaphore, _shutdown_event, _crawl_request_fn, _pool_getter
    global _unified_response_cls, _build_unified_result_fn, _get_metrics_fn
    _webhook_semaphore = None
    _shutdown_event = None
    _crawl_request_fn = None
    _pool_getter = None
    _unified_response_cls = None
    _build_unified_result_fn = None
    _get_metrics_fn = None


async def deliver_pinned_webhook(webhook_url: str, payload_bytes: bytes) -> None:
    """POST ``payload_bytes`` to ``webhook_url`` with DNS-rebinding safety.

    Plain HTTP connects to the pinned IP (resolved once) while preserving
    the original ``Host`` header so virtual-host routing still works.
    HTTPS instead re-validates the hostname fresh and POSTs to the original
    URL (L3): rewriting https to a raw IP breaks TLS verification (certs
    never carry IP SANs), so IP-pinning would make every HTTPS delivery
    fail. The residual rebinding window is the same posture the fetcher
    accepts for HTTPS (pre-connect check + CA validation).

    Caller MUST have validated ``webhook_url`` via ``validate_webhook_url``
    first; this helper assumes the URL has already passed the SSRF guard.

    Consolidated from ``run_and_deliver_webhook`` so the ``post_webhook``
    path shares the same TOCTOU-safe delivery code (adversarial review
    F-webhook-toctou).
    """
    import httpx

    from app.crawl.strategies.http import _check_ssrf_protection, _resolve_and_pin_host

    parsed = urlparse(webhook_url)
    request_headers = {"Content-Type": "application/json"}
    if parsed.scheme == "https":
        _check_ssrf_protection(webhook_url)
        pinned_url = webhook_url
    else:
        _hostname, pinned_ip = _resolve_and_pin_host(webhook_url)
        pinned_url = (
            f"{parsed.scheme}://{pinned_ip}:{parsed.port or (80 if parsed.scheme == 'http' else 443)}"
            f"{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")
        )
        if parsed.hostname:
            request_headers["Host"] = parsed.hostname
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(pinned_url, content=payload_bytes, headers=request_headers)


def _max_webhook_size() -> int:
    return int(os.environ.get("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "10485760"))


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
    """
    from app.crawl.strategies.http import _check_ssrf_protection
    from app.validation import parse_bool_env

    allow_private = parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False)
    parsed = urlparse(webhook_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webhook_url must use http or https scheme")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("webhook_url missing hostname")
    if allow_private:
        return
    if hostname in {"localhost", "host.docker.internal"}:
        raise ValueError("webhook_url points to private network target")
    _check_ssrf_protection(webhook_url)


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
            _unified_response_cls,
            _build_unified_result_fn,
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
    unified_results = [_build_unified_result_fn(item) for item in result.get("results", [])]
    response_payload = _unified_response_cls(
        request_id=request_id,
        submitted_count=result.get("submitted_count", len(unified_results)),
        policy_schema_version=result.get("policy_schema_version", ""),
        success=True,
        results=unified_results,
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
    except Exception:
        pass


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
