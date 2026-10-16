"""Shared payload parsing helpers for crawl entry points.

Centralizes URL extraction and policy normalization used by both
``app.orchestration.engine.crawl_request`` and the API routers that invoke it.
"""

from __future__ import annotations

from typing import Any

from app.admission.policy import PolicyTier, validate_wire_policy
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl


def _validated_url(url: str, *, host_policy: HostPolicy) -> str:
    try:
        return str(NormalizedUrl(url, policy=host_policy))
    except ValueError as error:
        reason = str(error)
        if reason in {"unsupported_scheme", "invalid_url"}:
            reason = "invalid_url"
        raise ValueError(reason) from error


def payload_urls(payload: dict[str, Any], *, host_policy: HostPolicy) -> list[str]:
    """The request URL batch, collapsed by page identity.

    Dedup keys on the :class:`NormalizedUrl` canonical form, first kept,
    order-preserving, validated first so no refused target crosses a boundary.
    """
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            key = _validated_url(url, host_policy=host_policy)
            if key not in seen:
                seen.add(key)
                unique.append(url)
        return unique
    if "url" in payload and isinstance(payload["url"], str):
        _validated_url(payload["url"], host_policy=host_policy)
        return [payload["url"]]
    raise ValueError("missing_url")


def checked_payload_urls(
    payload: dict[str, Any],
    max_batch_size: int,
    *,
    host_policy: HostPolicy,
    urls: list[str] | None = None,
) -> list[str]:
    """Deduplicated URL batch; rejected if it exceeds the batch cap.

    Engine (sync) and crawl endpoint (async accept) share this gate; ``urls``
    forwarding skips re-derivation but never the cap.
    """
    if urls is None:
        urls = payload_urls(payload, host_policy=host_policy)
    else:
        for url in urls:
            if not isinstance(url, str):
                raise ValueError("invalid_url")
            _validated_url(url, host_policy=host_policy)
    if len(urls) > max_batch_size:
        raise ValueError("batch_size_exceeded")
    return urls


def payload_policy(payload: dict[str, Any], tier: PolicyTier) -> CrawlPolicy:
    """Validate a wire policy into the model every decision site reads.

    ``invalid_policy_*`` codes come from where the model reports the error, in
    :func:`validate_wire_policy`; the rejection shape differs by edge.
    """
    return validate_wire_policy(payload.get("policy") or {}, tier)


__all__ = ["checked_payload_urls", "payload_policy", "payload_urls"]
