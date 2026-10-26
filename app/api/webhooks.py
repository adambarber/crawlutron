"""Webhook delivery helpers (SSRF-safe, IP-pinned): every outbound callback
routes through ``post_webhook``; deliveries are HMAC-signed, retried with
backoff, and drained on shutdown so a finished job's callback is never dropped.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import io
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.config.env import get_env_int, get_mapping_int, parse_bool_env
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.safety.bounded_io import BodyTooLarge, read_capped_async
from app.transport.timeouts import Timeouts

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Crawlutron-Webhook-Signature"
_TIMESTAMP_HEADER = "X-Crawlutron-Webhook-Timestamp"

_WEBHOOK_MAX_ATTEMPTS_DEFAULT = 3
_WEBHOOK_BACKOFF_MS_DEFAULT = 200

# In-flight delivery tasks, tracked so shutdown drains a callback even when the
# runner that scheduled it was cancelled after its terminal write landed.
_DELIVERIES: set[asyncio.Task] = set()

# (raw env value, parsed decision) for CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK.
# Value-keyed: an env change lands on the next call, an unchanged one skips it.
_PLAINTEXT_WEBHOOK_DECISION: tuple[str | None, bool] | None = None


class DeliveryFailed(Exception):
    """A webhook POST failed and either can be retried or is permanent."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def webhook_secret() -> str:
    """The shared HMAC key for outbound webhook signatures; empty disables signing."""
    return os.environ.get("CRAWLUTRON_WEBHOOK_SECRET", "")


def reset_plaintext_webhook_memo() -> None:
    """Drop the memoized plaintext-webhook env decision (tests)."""
    global _PLAINTEXT_WEBHOOK_DECISION
    _PLAINTEXT_WEBHOOK_DECISION = None


def allow_plaintext_webhooks() -> bool:
    """Memoized read of the plaintext-webhook escape hatch (inverted default)."""
    global _PLAINTEXT_WEBHOOK_DECISION
    raw = os.environ.get("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK")
    cached = _PLAINTEXT_WEBHOOK_DECISION
    if cached is not None and cached[0] == raw:
        return cached[1]
    decision = parse_bool_env("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", default=False)
    _PLAINTEXT_WEBHOOK_DECISION = (raw, decision)
    if decision:
        # Warned at the decision, not per call: the memo logs once per distinct
        # env value instead of flooding every admission.
        logger.warning(
            "Plaintext-webhook guard DISABLED by CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK=%r: "
            "HMAC signatures and crawl payloads ride unencrypted to caller-supplied "
            "http:// webhook URLs. Unset it, or set it to 0, outside local development.",
            raw,
            extra={"env_name": "CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "env_value": raw},
        )
    return decision


def ensure_https_webhook_url(webhook_url: str) -> None:
    """The one scheme law for both webhook doors (#499): plaintext http:// is
    refused unless CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK=1; raises ValueError."""
    if not webhook_url.lower().startswith("http://"):
        return
    if allow_plaintext_webhooks():
        return
    raise ValueError(
        "webhook_url must use https:// -- the HMAC signature and payload would ride "
        "plaintext http:// unencrypted (set CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK=1 to "
        "allow plaintext webhooks in local development)"
    )


def webhook_max_attempts() -> int:
    """How many times a webhook delivery is tried before giving up."""
    return get_env_int("CRAWLUTRON_WEBHOOK_MAX_ATTEMPTS", _WEBHOOK_MAX_ATTEMPTS_DEFAULT, lo=1)


def webhook_backoff_base_ms() -> int:
    """First retry delay in ms; each retry doubles it (exponential backoff)."""
    return get_env_int("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", _WEBHOOK_BACKOFF_MS_DEFAULT, lo=0)


async def deliver_pinned_webhook(
    webhook_url: str,
    payload_bytes: bytes,
    *,
    host_policy: HostPolicy,
) -> None:
    """POST ``payload_bytes`` to ``webhook_url`` with DNS-rebinding safety.
    Raises :class:`DeliveryFailed` on a non-2xx so the retry loop can act on it.
    """
    # one place) rather than urlparse/check/pin inline -- manual composition is the
    # shape that produced the redirect findings, and this closes validation's window.
    from app.transport.pinned_transport import build_pinned_request_async, sni_extension

    normalized = NormalizedUrl(webhook_url, policy=host_policy)
    connect_url, connect_headers = await build_pinned_request_async(
        normalized, {}, timeout_seconds=Timeouts.defaults().read
    )
    request_headers = {"Content-Type": "application/json", **connect_headers}
    secret = webhook_secret()
    if secret:
        request_headers[_SIGNATURE_HEADER] = _signature(payload_bytes, secret)
        request_headers[_TIMESTAMP_HEADER] = str(int(time.time()))
    # The pin connect URL now carries the validated IP for HTTPS too; keep the
    # original hostname as TLS SNI so real-site TLS still attests and routes.
    extensions = sni_extension(normalized.scheme, normalized.host)
    # Deliberate one-per-delivery cost: pooling would re-resolve by hostname (defeating
    # the pin) or cache the IP across deliveries (reopening the rebinding window).
    async with httpx.AsyncClient(timeout=Timeouts.defaults().as_httpx()) as client:
        async with client.stream(
            "POST",
            connect_url,
            content=payload_bytes,
            headers=request_headers,
            extensions=extensions,
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
            status = response.status_code
            if status >= 500 or status == 429:
                raise DeliveryFailed(f"transient webhook status {status}", retryable=True)
            if status >= 400:
                raise DeliveryFailed(f"webhook receiver refused ({status})", retryable=False)


def _signature(payload_bytes: bytes, secret: str) -> str:
    """HMAC-SHA256 of the exact body; the receiver recomputes over the same bytes."""
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _backoff_sleep(attempt: int, base_ms: int) -> None:
    """Exponential backoff: base * 2**attempt ms, never more than one minute."""
    delay = min(base_ms * (2**attempt), 60_000) / 1000.0
    await asyncio.sleep(delay)


async def drain_webhook_deliveries() -> None:
    """Await every in-flight delivery once (shutdown), so a cancelled runner's
    callback is delivered before the stores close."""
    for task in list(_DELIVERIES):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # The delivery already logged its loss; a re-raised error is drained.
            logger.warning("webhook_drain_error", extra={"error": str(exc)})


def _should_retry(delivery_exc: Exception, attempt: int, attempts: int) -> bool:
    """A retry/backoff decision: transient overflow-AX or a transport error retries."""
    transient = isinstance(delivery_exc, httpx.HTTPError) or (
        isinstance(delivery_exc, DeliveryFailed) and delivery_exc.retryable
    )
    return transient and attempt < attempts - 1


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


def _serialize_webhook_payload(payload: Mapping[str, object], cap: int) -> bytes | None:
    """The webhook body as bytes, or ``None`` once it passes ``cap`` -- the
    running buffer is the only whole-payload copy, so a capped payload never
    holds the full str and the full bytes at once (#342)."""
    buffer = io.BytesIO()
    for chunk in json.JSONEncoder().iterencode(payload):
        buffer.write(chunk.encode("utf-8"))
        if buffer.tell() > cap:
            return None
    return buffer.getvalue()


async def post_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    host_policy: HostPolicy,
) -> None:
    """POST a webhook (SSRF/IP pinned, HMAC-signed, size-capped) with backoff retries.
    Best-effort: nothing raises -- a lost callback is logged, so it cannot corrupt
    an already-recorded outcome (#450); validation lives in the pin per attempt.
    """
    cap = max_webhook_size()
    payload_bytes = _serialize_webhook_payload(payload, cap)
    if payload_bytes is None:
        # The exact size is never materialised; the cap floor is what is known.
        logger.warning(
            "webhook_payload_too_large",
            extra={"url": webhook_url, "size": cap},
        )
        return
    attempts = webhook_max_attempts()
    for attempt in range(attempts):
        try:
            await deliver_pinned_webhook(webhook_url, payload_bytes, host_policy=host_policy)
            return
        except Exception as exc:
            if not _should_retry(exc, attempt, attempts):
                logger.warning(
                    "webhook_delivery_failed",
                    extra={"url": webhook_url, "error": str(exc), "attempt": attempt + 1},
                )
                return
            await _backoff_sleep(attempt, webhook_backoff_base_ms())


def _tracked_delivery(
    webhook_url: str,
    payload: Mapping[str, object],
    *,
    host_policy: HostPolicy,
) -> asyncio.Task:
    """Spawn one delivery joined to the shutdown drain set, spawn site agnostic."""
    task = asyncio.create_task(post_webhook(webhook_url, payload, host_policy=host_policy))
    _DELIVERIES.add(task)
    task.add_done_callback(_DELIVERIES.discard)
    return task


async def post_webhook_guarded(
    webhook_url: str,
    payload: Mapping[str, object],
    *,
    host_policy: HostPolicy,
) -> None:
    """Await ``post_webhook`` as a shielded, drained task: a runner cancelled at
    shutdown does not drop the callback whose terminal write already landed."""
    task = _tracked_delivery(webhook_url, payload, host_policy=host_policy)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The runner was cancelled; the tracked delivery continues for drain.
        raise


def start_webhook_delivery(
    webhook_url: str,
    payload: Mapping[str, object],
    *,
    host_policy: HostPolicy,
) -> asyncio.Task:
    """Spawn the delivery as a tracked background task and return at once.

    The sweep pass must not stall on a slow receiver (#528); the task joins the
    same ``_DELIVERIES`` set, so shutdown drains it like any other callback."""
    return _tracked_delivery(webhook_url, payload, host_policy=host_policy)


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
    "ensure_https_webhook_url",
    "max_webhook_size",
    "post_webhook",
    "reset_plaintext_webhook_memo",
    "validate_webhook_url",
    "validate_webhook_url_async",
]
