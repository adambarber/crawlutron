"""Delta crawl classification and accounting: one pure source of truth.

Classifies each crawl result against its caller-isolated baseline evidence
(validators, content fingerprint, freshness) and rolls a shaped batch up into
``delta_summary``. No I/O, no network, no facade logic: doors thread the
request knobs (``changed_since``, ``baseline_request_id``) and the loaded
baseline in through :class:`DeltaContext`. Unset knobs mean no classification
and no wire change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from hashlib import sha256

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult

#: Wire spellings of the four verdicts. ``UnifiedCrawlResult.delta_classification``
#: is ``str | None`` so these constants are the single spelling source; the wire
#: never sees a classification when no delta knob was set.
NEW = "new"
CHANGED = "changed"
UNCHANGED = "unchanged"
REMOVED = "removed"

_NOT_MODIFIED = "not_modified"


@dataclass(frozen=True, slots=True)
class BaselineEvidence:
    """Per-URL evidence from the baseline request or per-URL submitted validators.

    ``etag``/``last_modified`` are origin validators a conditional revalidation
    can be sent against; ``fingerprint`` is the content fingerprint, or None.
    """

    etag: str | None = None
    last_modified: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class DeltaContext:
    """Caller-scoped delta request: knobs plus the resolved baseline.

    ``baseline_expired`` is true when ``baseline_request_id`` was set but the
    stored row was missing, expired, or foreign to this caller: the request
    degrades to a full crawl and every result classifies ``new`` with
    ``why: "baseline_expired"``.
    """

    changed_since: float | None
    baseline_request_id: str | None
    baseline: dict[str, BaselineEvidence]
    baseline_expired: bool


def _url_key(item: EngineCrawlResult) -> str:
    """The admitted identity to look a result's baseline up under."""
    if item.normalized_url is not None:
        return str(item.normalized_url)
    return item.original_url


def content_fingerprint(item: EngineCrawlResult) -> str | None:
    """A lightweight fingerprint of this result's delivered content, or None.

    Only content this run already fetched is hashed; an empty delivery reports
    None honestly instead of inventing a stable fingerprint of nothing.
    """
    return _content_fingerprint(item.markdown, item.html, item.text)


def _content_fingerprint(markdown: str | None, html: str | None, text: str | None) -> str | None:
    parts = [part for part in (markdown, html, text) if part]
    if not parts:
        return None
    return sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _http_date_epoch(value: str | None) -> float | None:
    """Epoch seconds for an HTTP date header, or None when it cannot be parsed."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _content_cache_miss(item: EngineCrawlResult) -> bool:
    """Whether this run's diagnostics prove nothing was served from cache."""
    retry = item.diagnostics.get("retry") if isinstance(item.diagnostics, Mapping) else None
    return isinstance(retry, Mapping) and retry.get("content_cache") == "miss"


def _current_validators(item: EngineCrawlResult) -> tuple[str | None, str | None]:
    """The origin validators this result landed with, or an empty pair."""
    if not item.validators:
        return (None, None)
    return (item.validators.get("etag"), item.validators.get("last_modified"))


def classify_result(
    item: EngineCrawlResult,
    *,
    context: DeltaContext | None,
) -> tuple[str | None, dict[str, object] | None]:
    """Classify one result against its caller-isolated baseline, or (None, None).

    Fail-open and honest: never ``unchanged`` without evidence. The baseline is
    already caller-scoped by the loader, so nothing here can leak across callers.
    """
    if context is None or (context.changed_since is None and context.baseline_request_id is None):
        return None, None
    if context.baseline_expired:
        return NEW, _evidence("baseline_expired", item, context=context)
    baseline = context.baseline.get(_url_key(item))
    status = item.http_status
    if item.classification == _NOT_MODIFIED or status == 304:
        if baseline is not None:
            return UNCHANGED, _evidence(
                "304_not_modified", item, baseline=baseline, context=context, matched_validators=True
            )
        return NEW, _evidence("304_without_baseline", item, context=context)
    if status == 200:
        if baseline is not None:
            return _classify_validated_200(item, baseline, context)
        return _classify_by_freshness(item, context.changed_since)
    if baseline is not None:
        return CHANGED, _evidence("origin_status_unusable", item, baseline=baseline, context=context)
    return _classify_by_freshness(item, context.changed_since)


def _classify_validated_200(
    item: EngineCrawlResult,
    baseline: BaselineEvidence,
    context: DeltaContext,
) -> tuple[str, dict[str, object]]:
    """A 200 against a baseline: fingerprints decide, validators confirm."""
    current_hash = content_fingerprint(item)
    if current_hash is not None and baseline.fingerprint is not None:
        if current_hash == baseline.fingerprint:
            return UNCHANGED, _evidence("same_fingerprint", item, baseline=baseline, context=context)
        return CHANGED, _evidence("fingerprint_diff", item, baseline=baseline, context=context)
    current = _current_validators(item)
    baseline_pair = (baseline.etag, baseline.last_modified)
    if current != (None, None) or baseline_pair != (None, None):
        if current == baseline_pair:
            return UNCHANGED, _evidence("validators_match", item, baseline=baseline, context=context)
        return CHANGED, _evidence("validators_diff", item, baseline=baseline, context=context)
    return CHANGED, _evidence("evidence_incomplete", item, baseline=baseline, context=context)


def _classify_by_freshness(
    item: EngineCrawlResult, changed_since: float | None
) -> tuple[str, dict[str, object]]:
    """No baseline evidence: ``changed_since`` freshness, fail open toward change."""
    if changed_since is None:
        return NEW, _evidence("no_baseline", item, context=None)
    last_modified = _http_date_epoch(item.headers.get("last-modified")) if item.headers else None
    if last_modified is not None:
        if last_modified > changed_since:
            return CHANGED, _evidence("last_modified_newer", item, context=None)
        return UNCHANGED, _evidence("last_modified_not_newer", item, context=None)
    if _content_cache_miss(item):
        return NEW, _evidence("nothing_cached", item, context=None)
    return CHANGED, _evidence("freshness_undeterminable", item, context=None)


def _evidence(
    why: str,
    item: EngineCrawlResult,
    *,
    baseline: BaselineEvidence | None = None,
    context: DeltaContext | None = None,
    matched_validators: bool = False,
) -> dict[str, object]:
    """The evidence envelope for one verdict: what was compared and what said so."""
    evidence: dict[str, object] = {"why": why, "http_status": item.http_status}
    if item.validators:
        evidence["etag"] = item.validators.get("etag")
        evidence["last_modified"] = item.validators.get("last_modified")
    if baseline is not None:
        evidence["baseline_etag"] = baseline.etag
        evidence["baseline_last_modified"] = baseline.last_modified
        if baseline.fingerprint is not None:
            evidence["baseline_content_hash"] = baseline.fingerprint
    fingerprint = content_fingerprint(item)
    if fingerprint is not None:
        evidence["content_hash"] = fingerprint
    if context is not None and context.baseline_request_id is not None:
        evidence["baseline_request_id"] = context.baseline_request_id
    if matched_validators:
        evidence["matched_validators"] = True
    return evidence


def delta_summary_for(
    shaped: list[UnifiedCrawlResult],
    *,
    removed_urls: tuple[str, ...] = (),
    delta_requested: bool = False,
) -> dict[str, object] | None:
    """Deterministic rollup of one shaped batch, or None when delta was not asked.

    ``delta_requested`` is the caller's signal that a delta knob was set; without
    it there is no summary to emit. ``considered`` is every shaped result;
    buckets count only classified ones, so an unclassifiable result (a shaping
    failure) is considered but in no bucket, and is listed in ``skipped`` with
    its why.
    """
    if not delta_requested:
        return None
    counts: dict[str, int] = {NEW: 0, CHANGED: 0, UNCHANGED: 0, REMOVED: 0}
    skipped: list[dict[str, str]] = []
    for result in shaped:
        classification = result.delta_classification
        url = str(result.normalized_url or result.original_url)
        if classification is None:
            evidence = result.delta_evidence or {}
            skipped.append({"url": url, "why": str(evidence.get("why", "classification_unavailable"))})
            continue
        counts[classification] = counts.get(classification, 0) + 1
        if classification == UNCHANGED:
            skipped.append({"url": url, "why": str((result.delta_evidence or {}).get("why", "unchanged"))})
    counts[REMOVED] = len(removed_urls)
    skipped.extend({"url": url, "why": "not_in_admitted_set"} for url in removed_urls)
    return {
        "considered": len(shaped),
        "new": counts[NEW],
        "changed": counts[CHANGED],
        "unchanged": counts[UNCHANGED],
        "removed": counts[REMOVED],
        "skipped": skipped,
    }


def extract_baseline(stored_payload: dict[str, object]) -> dict[str, BaselineEvidence]:
    """Per-URL baseline evidence from a stored ``UnifiedCrawlResponse`` dump.

    Keys are every identity the stored result carried (normalized URL first,
    then the caller's original text) so a later crawl matches whichever identity
    its result answers under.
    """
    baseline: dict[str, BaselineEvidence] = {}
    results = stored_payload.get("results")
    if not isinstance(results, list):
        return baseline
    for result in results:
        if not isinstance(result, dict):
            continue
        normalized = result.get("normalized_url")
        original = result.get("original_url")
        keys = [key for key in (normalized, original) if isinstance(key, str) and key]
        if not keys:
            continue
        validators = result.get("validators")
        etag: str | None = None
        last_modified: str | None = None
        if isinstance(validators, Mapping):
            etag_value = validators.get("etag")
            last_modified_value = validators.get("last_modified")
            etag = etag_value if isinstance(etag_value, str) else None
            last_modified = last_modified_value if isinstance(last_modified_value, str) else None
        markdown = result.get("markdown")
        html = result.get("html")
        text = result.get("text")
        evidence = BaselineEvidence(
            etag=etag,
            last_modified=last_modified,
            fingerprint=_content_fingerprint(
                markdown if isinstance(markdown, str) else None,
                html if isinstance(html, str) else None,
                text if isinstance(text, str) else None,
            ),
        )
        for key in keys:
            baseline[key] = evidence
    return baseline


def removed_urls_for(submitted_urls: Iterable[str], admitted_identities: Iterable[str]) -> tuple[str, ...]:
    """Submitted URLs absent from this run's admitted set.

    Only honest when the caller submitted a bounded explicit set and admission
    decides per URL; an empty admitted delta means nothing was removed this run.
    """
    admitted = set(admitted_identities)
    return tuple(url for url in submitted_urls if url not in admitted)


__all__ = [
    "BaselineEvidence",
    "CHANGED",
    "DeltaContext",
    "NEW",
    "REMOVED",
    "UNCHANGED",
    "classify_result",
    "content_fingerprint",
    "delta_summary_for",
    "extract_baseline",
    "removed_urls_for",
]
