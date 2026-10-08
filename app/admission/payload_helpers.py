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


def _payload_urls(payload: dict[str, Any], *, host_policy: HostPolicy) -> list[str]:
    """Extract the request URL batch, collapsing duplicates by page identity.

    ``NormalizedUrl`` is the single source of truth for what counts
    as the "same page" (fragment / trailing-slash / tracking-param / param-order
    variants all canonicalize identically), so dedup here must key on that
    canonical form -- not the raw string. Identical pages crawl once; the first
    occurrence is kept, order-preserving. Both the engine and the render paths
    inherit the dedup here, and the batch-size check downstream counts unique
    pages. Validation happens before the key is built, so a refused target is
    never a distinct entry that can cross an acceptance boundary.
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
    """Deduplicated URL batch, rejected if it exceeds the batch cap.

    The single gate shared by the engine (sync) and the crawl endpoint (async
    accept path): the async handler must call this *before* returning 202 so
    an oversized batch is rejected at the boundary instead of being accepted
    and failing inside the detached background task, where the caller can no
    longer see why.

    ``urls`` carries an already-derived batch forward: the
    endpoint gate parses the payload once and hands the result to the engine,
    so a request never parses the same batch twice. A forwarded batch skips
    re-derivation but not the cap -- supplying urls cannot smuggle an
    oversized batch past the ceiling.
    """
    if urls is None:
        urls = _payload_urls(payload, host_policy=host_policy)
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

    The ``invalid_policy_*`` codes this raises are derived from where the model
    reports the error, in :func:`app.admission.policy.validate_wire_policy`, rather
    than spelled out here -- the hand-spelled version that used to live in this
    function had already begun to read ``limits`` three different ways and to
    enforce bounds the model did not declare.

    How a caller sees a rejection is not decided here either, and the honest
    description is that it differs by edge. Over HTTP, FastAPI validates
    ``CrawlRequest.policy`` as this model *before* the handler runs, so a
    rejected policy arrives as a 422 ``validation_error`` carrying pydantic's
    own ``loc``-tagged message and never reaches this function -- which makes
    the per-field codes unreachable on that path, not merely unused. MCP
    surfaces the bare code string. A direct ``crawl_request`` caller gets the
    ``ValueError``. Three shapes for one bad input, each owned by the edge that
    produced it.
    """
    return validate_wire_policy(payload.get("policy") or {}, tier)


__all__ = ["checked_payload_urls", "payload_policy"]
