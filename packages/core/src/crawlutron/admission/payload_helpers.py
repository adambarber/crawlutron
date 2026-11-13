"""Shared payload parsing helpers for crawl entry points.

Centralizes URL extraction and policy normalization used by both
``crawlutron.orchestration.engine.crawl_request`` and the API routers that invoke it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from safe_url.normalized_url import NormalizedUrl

from crawlutron.admission.policy import PolicyTier, validate_wire_policy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.single_url_source_request import SingleUrlSourceRequest

if TYPE_CHECKING:
    from safe_url.host_policy import HostPolicy

    from crawlutron.models.crawl_policy import CrawlPolicy


def _validated_url(url: str, *, host_policy: HostPolicy) -> NormalizedUrl:
    try:
        return NormalizedUrl(url, policy=host_policy)
    except ValueError as error:
        reason = str(error)
        if reason in {"unsupported_scheme", "invalid_url"}:
            reason = "invalid_url"
        raise ValueError(reason) from error


def _single_or_batch_urls(payload: SingleUrlSourceRequest) -> list[str]:
    """The URL list a request carries -- ``urls`` else ``[url]``. The source contract
    (``SingleUrlSourceRequest``) enforces at least one URL at construction, so this
    never comes up empty -- shared by crawl and render, which follow the same door.
    """
    if payload.urls is not None:
        return payload.urls
    if payload.url is None:
        raise ValueError("missing_url")
    return [payload.url]


def checked_payload_urls(
    payload: SingleUrlSourceRequest,
    max_batch_size: int,
    *,
    host_policy: HostPolicy,
    url_pairs: list[tuple[str, NormalizedUrl]] | None = None,
) -> list[tuple[str, NormalizedUrl]]:
    """The admitted batch as ``(original_text, NormalizedUrl)`` pairs, deduped on
    admitted identity and capped; the value is kept so consumers never rebuild it.
    Forwarded ``url_pairs`` are the door's own admission, capped not re-derived.
    """
    if url_pairs is not None:
        for text, normalized in url_pairs:
            if not isinstance(text, str) or not isinstance(normalized, NormalizedUrl):
                raise ValueError("invalid_url")
        batch = list(url_pairs)
    else:
        seen: set[str] = set()
        batch = []
        for url in _single_or_batch_urls(payload):
            normalized = _validated_url(url, host_policy=host_policy)
            key = str(normalized)
            if key not in seen:
                seen.add(key)
                batch.append((url, normalized))
    if len(batch) > max_batch_size:
        raise ValueError("batch_size_exceeded")
    return batch


def payload_policy(payload: CrawlRequest, tier: PolicyTier) -> CrawlPolicy:
    """The validated crawl policy the request model carries, tier defaults absorbed.

    The typed funnel keeps the wire path: the unverified wire ``policy`` is
    validated here (not after coercion), and only the model crosses downstream.
    """
    if isinstance(payload, CrawlRequest):
        return validate_wire_policy(payload.policy.model_dump(), tier)
    raise TypeError("payload_policy takes a CrawlRequest; validate the wire payload first")


__all__ = ["checked_payload_urls", "payload_policy"]
