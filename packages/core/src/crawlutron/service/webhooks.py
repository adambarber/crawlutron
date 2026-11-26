"""Webhook delivery helpers (SSRF-safe, IP-pinned): every outbound callback
routes through ``post_webhook``; deliveries are HMAC-signed, retried with
backoff, and drained on shutdown so a finished job's callback survives.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import io
import json
import logging
import time
from collections.abc import Mapping
from urllib.parse import urlparse

from httpx import HTTPError

from crawlutron.config.env import get_env_int
from crawlutron.config.settings_cache import get_cached_settings
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.runtime import process_state
from crawlutron.safety.bounded_io import BodyTooLarge, read_capped_async
from crawlutron.transport.http_client_pool import get_async_http_client
from crawlutron.transport.timeouts import Timeouts
from crawlutron.transport.transport import FetchRequest
from crawlutron_admit import Count, Duration
from crawlutron_admit.task_scope import Grace, Handle, NamedRegistry, ScopeRefused, TaskScope
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl
from safe_url.url_redaction import redact_urls_in_text

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Crawlutron-Webhook-Signature"
_TIMESTAMP_HEADER = "X-Crawlutron-Webhook-Timestamp"

_WEBHOOK_MAX_ATTEMPTS_DEFAULT = 3
_WEBHOOK_BACKOFF_MS_DEFAULT = 200

# In-flight delivery tasks, tracked so shutdown drains a callback even when the
# runner was cancelled after its terminal write landed. Hard cap (#655).
DELIVERIES_CAP = 1024
_DELIVERY_SCOPE = TaskScope.process(
    registry=NamedRegistry("webhook:delivery", Count(DELIVERIES_CAP), Duration(0.25)),
    grace=Grace(1.0),
)
_DELIVERIES: set[Handle] = set()

#: One-time flag: the first delivery under an unset secret warns once (#655),
#: not once per delivery -- an unsigned deployment would otherwise flood the log.
_UNSIGNED_WARNING_EMITTED = False


class DeliveryFailed(Exception):
    """A webhook POST failed and either can be retried or is permanent."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def webhook_secret() -> str:
    """The shared HMAC key for outbound webhook signatures; empty disables signing."""
    return get_cached_settings().webhook_secret


def _reset() -> None:
    """Cancel in-flight deliveries and empty the tracked set."""
    global _UNSIGNED_WARNING_EMITTED
    for handle in list(_DELIVERIES):
        _DELIVERY_SCOPE.stop_now(handle)
    _DELIVERIES.clear()
    _UNSIGNED_WARNING_EMITTED = False


def _release_delivery(handle: Handle, _task: asyncio.Task) -> None:
    """Done callback: drop the handle and return its admission slot."""
    _DELIVERIES.discard(handle)
    _DELIVERY_SCOPE.stop_now(handle)


def _has() -> bool:
    return bool(_DELIVERIES)


process_state.register(__name__, _reset, _has)


def ensure_https_webhook_url(webhook_url: str) -> None:
    """The one scheme law for both webhook doors: https-only (#651);
    raises ValueError unless the parsed scheme is exactly https."""
    if urlparse(webhook_url).scheme != "https":
        raise ValueError(
            "webhook_url must use https:// -- the HMAC signature and payload would ride an unencrypted hop"
        )


def webhook_max_attempts() -> int:
    """How many times a webhook delivery is tried before giving up."""
    return get_env_int("CRAWLUTRON_WEBHOOK_MAX_ATTEMPTS", _WEBHOOK_MAX_ATTEMPTS_DEFAULT, lo=1)


def webhook_backoff_base_ms() -> int:
    """First retry delay in ms; each retry doubles it (exponential backoff)."""
    return get_env_int("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", _WEBHOOK_BACKOFF_MS_DEFAULT, lo=0)


def webhook_drain_deadline_seconds() -> float:
    """The total shutdown-drain budget: one delivery's worst path --
    every attempt, plus its backoffs -- from the existing retry budget."""
    attempts = webhook_max_attempts()
    base_s = webhook_backoff_base_ms() / 1000.0
    backoff_total = sum(min(base_s * (2**a), 60.0) for a in range(attempts - 1))
    return attempts * Timeouts.defaults().read + backoff_total


async def deliver_pinned_webhook(
    webhook_url: str,
    payload_bytes: bytes,
    *,
    host_policy: HostPolicy,
) -> None:
    """POST ``payload_bytes`` to ``webhook_url`` with DNS-rebinding safety.
    Raises :class:`DeliveryFailed` on a non-2xx so the retry loop can act on it.
    """
    # one place) rather than urlparse/check inline -- manual composition is the
    # shape that produced the redirect findings, and this closes validation's window.
    normalized = NormalizedUrl(webhook_url, policy=host_policy)
    request_headers = {"Content-Type": "application/json"}
    secret = webhook_secret()
    if secret:
        # wall: the HMAC replay window is the receiver's wall clock.
        timestamp = str(int(time.time()))
        request_headers[_SIGNATURE_HEADER] = _signature(payload_bytes, secret, timestamp)
        request_headers[_TIMESTAMP_HEADER] = timestamp
    else:
        _warn_unsigned_once()
    # The proxy owns target admission, fresh DNS and the only dial; the pooled
    # client carries the logical URL and reuses the connection safely.
    client = get_async_http_client()
    request = FetchRequest(
        headers=request_headers,
        url=normalized,
        timeout_seconds=Timeouts.defaults().read,
    )
    async with client.stream(
        "POST",
        request,
        content=payload_bytes,
        headers=request_headers,
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
        # 3xx is not delivery: the receiver never gave a final verdict at the
        # callback URL, so it follows the transient policy and is retried.
        if 300 <= status < 400 or status >= 500 or status == 429:
            raise DeliveryFailed(f"transient webhook status {status}", retryable=True)
        if status >= 400:
            raise DeliveryFailed(f"webhook receiver refused ({status})", retryable=False)


def _warn_unsigned_once() -> None:
    """One WARNING naming the env var at the first unsigned delivery (#655); the
    delivery itself never fails closed -- signing is opt-in configuration."""
    global _UNSIGNED_WARNING_EMITTED
    if _UNSIGNED_WARNING_EMITTED:
        return
    _UNSIGNED_WARNING_EMITTED = True
    logger.warning(
        "webhook_unsigned_delivery: %s unset; signature headers omitted",
        "CRAWLUTRON_WEBHOOK_SECRET",
    )


def _signature(payload_bytes: bytes, secret: str, timestamp: str) -> str:
    """HMAC-SHA256 binding the timestamp into the digest: the receiver recomputes
    over ``timestamp + "." + body`` (both from the delivery), so a captured
    signature cannot be replayed outside the timestamp window."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _backoff_sleep(attempt: int, base_ms: int) -> None:
    """Exponential backoff: base * 2**attempt ms, never more than one minute."""
    delay = min(base_ms * (2**attempt), 60_000) / 1000.0
    await asyncio.sleep(delay)


async def drain_webhook_deliveries() -> None:
    """Await every in-flight delivery once (shutdown), so a cancelled runner's
    callback is delivered before the stores close -- under a total deadline:
    one hung receiver must not wedge shutdown past the delivery budget.
    """
    handles = list(_DELIVERIES)
    if not handles:
        return
    tasks = [handle.task for handle in handles]
    try:
        _done, pending = await asyncio.wait(tasks, timeout=webhook_drain_deadline_seconds())
    except asyncio.CancelledError:
        raise
    for handle in handles:
        if handle.task in pending:
            try:
                await _DELIVERY_SCOPE.stop(handle, Grace(1.0))
            except ScopeRefused:
                pass
            # The delivery already logged its loss; abandonment is named here.
            logger.warning("webhook_drain_abandoned", extra={"task": repr(handle.task)})
        else:
            _DELIVERY_SCOPE.stop_now(handle)
            try:
                handle.task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # The delivery already logged its loss; a re-raised error is drained.
                logger.warning("webhook_drain_error", extra={"error": redact_urls_in_text(str(exc))})


def _should_retry(delivery_exc: Exception, attempt: int, attempts: int) -> bool:
    """A retry/backoff decision: transient overflow-AX or a transport error retries."""
    transient = isinstance(delivery_exc, HTTPError) or (
        isinstance(delivery_exc, DeliveryFailed) and delivery_exc.retryable
    )
    return transient and attempt < attempts - 1


def max_webhook_size() -> int:
    """The outbound webhook size cap, bounded and parsed at the settings home."""
    return get_cached_settings().max_webhook_size


def _serialize_webhook_payload(payload: Mapping[str, object], cap: int) -> bytes | None:
    """The webhook body as bytes, or ``None`` once it passes ``cap`` -- the
    running buffer is the only whole-payload copy, so a capped payload never
    holds the full str and the full bytes at once."""
    buffer = io.BytesIO()
    for chunk in json.JSONEncoder().iterencode(payload):
        buffer.write(chunk.encode("utf-8"))
        if buffer.tell() > cap:
            return None
    return buffer.getvalue()


async def post_webhook(
    target: WebhookTarget,
    payload: Mapping[str, object],
) -> None:
    """POST a webhook (admitted, HMAC-signed, size-capped) with backoff retries.
    Best-effort: nothing raises -- a lost callback is logged, so it cannot corrupt
    an already-recorded outcome; admission lived in the target's constructor.
    """
    webhook_url = target.delivery_url()
    try:
        ensure_https_webhook_url(webhook_url)
    except ValueError:
        logger.warning("webhook_http_refused", extra={"url": target.redacted()})
        return
    try:
        cap = max_webhook_size()
        payload_bytes = _serialize_webhook_payload(payload, cap)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "webhook_payload_serialization_failed",
            extra={"url": target.redacted(), "error": redact_urls_in_text(str(exc))},
        )
        return
    if payload_bytes is None:
        # The exact size is never materialised; the cap floor is what is known.
        logger.warning(
            "webhook_payload_too_large",
            extra={"url": target.redacted(), "size": cap},
        )
        return
    attempts = webhook_max_attempts()
    for attempt in range(attempts):
        try:
            await deliver_pinned_webhook(webhook_url, payload_bytes, host_policy=target.target.policy)
            return
        except Exception as exc:
            if not _should_retry(exc, attempt, attempts):
                logger.warning(
                    "webhook_delivery_failed",
                    extra={
                        "url": target.redacted(),
                        "error": redact_urls_in_text(str(exc)),
                        "attempt": attempt + 1,
                    },
                )
                return
            await _backoff_sleep(attempt, webhook_backoff_base_ms())


def _tracked_delivery(
    target: WebhookTarget,
    payload: Mapping[str, object],
) -> asyncio.Task | None:
    """Spawn one delivery joined to the shutdown drain set, spawn site agnostic.

    At the cap the delivery is refused (logged, best-effort) instead of
    tracked -- an unbounded set would grow without limit (#655)."""
    if len(_DELIVERIES) >= DELIVERIES_CAP:
        logger.warning(
            "webhook_delivery_refused_at_cap",
            extra={"cap": DELIVERIES_CAP, "url": target.redacted()},
        )
        return None
    try:
        handle = _DELIVERY_SCOPE.spawn_now(lambda: post_webhook(target, payload))
    except ScopeRefused:
        logger.warning("webhook_delivery_refused_at_cap", extra={"cap": DELIVERIES_CAP})
        return None
    _DELIVERIES.add(handle)
    handle.task.add_done_callback(functools.partial(_release_delivery, handle))
    return handle.task


async def post_webhook_guarded(
    target: WebhookTarget,
    payload: Mapping[str, object],
) -> None:
    """Await ``post_webhook`` as a shielded, drained task: a runner cancelled at
    shutdown does not drop the callback whose terminal write already landed."""
    task = _tracked_delivery(target, payload)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The runner was cancelled; the tracked delivery continues for drain.
        raise


def start_webhook_delivery(
    target: WebhookTarget,
    payload: Mapping[str, object],
) -> asyncio.Task | None:
    """Spawn the delivery as a tracked background task and return at once.

    The sweep pass must not stall on a slow receiver; the task joins ``_DELIVERIES``
    so shutdown drains it. ``None``: refused at the in-flight cap (#655)."""
    return _tracked_delivery(target, payload)


async def admit_webhook_target(webhook_url: str, *, host_policy: HostPolicy) -> WebhookTarget:
    """One chokepoint: validate (https, SSRF) then admit (no userinfo/secret
    params). Returns the validated, admitted target."""
    ensure_https_webhook_url(webhook_url)
    await validate_webhook_url_async(webhook_url, host_policy=host_policy)
    return WebhookTarget.admit(webhook_url, host_policy=host_policy)


async def validate_webhook_url_async(webhook_url: str, *, host_policy: HostPolicy) -> None:
    """Admit the webhook URL's shape and policy; target DNS and dial belong to
    the proxy, so no resolution happens here. Raises ValueError if unsafe.
    """
    NormalizedUrl(webhook_url, policy=host_policy)


__all__ = [
    "DELIVERIES_CAP",
    "admit_webhook_target",
    "deliver_pinned_webhook",
    "ensure_https_webhook_url",
    "max_webhook_size",
    "post_webhook",
    "validate_webhook_url_async",
]
