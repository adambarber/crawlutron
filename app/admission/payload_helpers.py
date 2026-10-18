"""Shared payload parsing helpers for crawl entry points.

Centralizes URL extraction and policy normalization used by both
``app.orchestration.engine.crawl_request`` and the API routers that invoke it.
"""

from __future__ import annotations

from typing import Any

from app.admission.crawl_surface import CrawlPolicy, CrawlRequest, HostPolicy, NormalizedUrl
from app.admission.policy import PolicyTier, validate_wire_policy


def _validated_url(url: str, *, host_policy: HostPolicy) -> str:
    try:
        return str(NormalizedUrl(url, policy=host_policy))
    except ValueError as error:
        reason = str(error)
        if reason in {"unsupported_scheme", "invalid_url"}:
            reason = "invalid_url"
        raise ValueError(reason) from error


def _single_or_batch_urls(payload: CrawlRequest) -> list[str]:
    """The URL list a request carries -- ``urls`` else ``[url]``. ``CrawlRequest``
    enforces at least one URL at construction, so this never comes up empty.
    """
    if payload.urls is not None:
        return payload.urls
    if payload.url is None:
        raise ValueError("missing_url")
    return [payload.url]


def checked_payload_urls(
    payload: CrawlRequest,
    max_batch_size: int,
    *,
    host_policy: HostPolicy,
    urls: list[str] | None = None,
) -> list[str]:
    """Deduplicated URL batch, collapsed by page identity, validated first, and
    rejected if it exceeds the batch cap. ``urls`` forwarding skips re-derivation
    (it still caps and validates) -- the one rule engine and every router share.
    """
    if urls is not None:
        for url in urls:
            if not isinstance(url, str):
                raise ValueError("invalid_url")
            _validated_url(url, host_policy=host_policy)
        batch = urls
    else:
        seen: set[str] = set()
        batch = []
        for url in _single_or_batch_urls(payload):
            key = _validated_url(url, host_policy=host_policy)
            if key not in seen:
                seen.add(key)
                batch.append(url)
    if len(batch) > max_batch_size:
        raise ValueError("batch_size_exceeded")
    return batch


def payload_policy(payload: CrawlRequest | dict[str, Any], tier: PolicyTier) -> CrawlPolicy:
    """The validated crawl policy a request carries, tier defaults absorbed.

    The ``dict`` arm is a documented wire-dict tolerance valve: the unverified
    wire ``policy`` is validated here (not after coercion), keeping the wire path.
    """
    raw = payload.policy.model_dump() if isinstance(payload, CrawlRequest) else payload.get("policy") or {}
    return validate_wire_policy(raw, tier)


__all__ = ["checked_payload_urls", "payload_policy"]
