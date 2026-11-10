"""Unit tests for crawlutron.delta: pure classification and accounting.

Every record is built through the shared ``engine_result`` primitive, so the
classification reads the same shapes production hands it.
"""

from __future__ import annotations

from dataclasses import replace

from crawlutron.delta import (
    CHANGED,
    NEW,
    UNCHANGED,
    BaselineEvidence,
    DeltaContext,
    classify_result,
    content_fingerprint,
    delta_summary_for,
    extract_baseline,
    removed_urls_for,
)
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.fetch_envelope import RetryDiagnostics
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from tests._result_builders import engine_result

ETAG_V1 = '"v1"'
URL = "http://example.com/"


def _context(
    *,
    changed_since: float | None = None,
    baseline_request_id: str | None = None,
    baseline: dict[str, BaselineEvidence] | None = None,
    baseline_expired: bool = False,
) -> DeltaContext:
    return DeltaContext(
        changed_since=changed_since,
        baseline_request_id=baseline_request_id,
        baseline=baseline or {},
        baseline_expired=baseline_expired,
    )


def _classify(
    result: EngineCrawlResult, context: DeltaContext | None
) -> tuple[str | None, dict[str, object]]:
    """``classify_result`` with the evidence's None branch narrowed away."""
    classification, evidence = classify_result(result, context=context)
    assert evidence is not None, "a classified result always carries evidence"
    return classification, evidence


def _baseline_evidence(
    etag: str | None = ETAG_V1, last_modified: str | None = None, fingerprint: str | None = None
) -> dict[str, BaselineEvidence]:
    """One-URL baseline map; the usual overrides are ``etag``/``fingerprint``."""
    return {URL: BaselineEvidence(etag=etag, last_modified=last_modified, fingerprint=fingerprint)}


def test_no_knobs_mean_no_classification():
    assert classify_result(engine_result(URL, "rendered"), context=None) == (None, None)
    assert classify_result(
        engine_result(URL, "rendered"),
        context=_context(changed_since=None, baseline_request_id=None),
    ) == (None, None)


def test_missing_expired_foreign_baseline_degrades_to_new():
    expired = _context(baseline_request_id="req-1", baseline_expired=True)
    for status in (200, 404, 304):
        classification, evidence = _classify(engine_result(URL, "rendered", http_status=status), expired)
        assert classification == NEW
        assert evidence["why"] == "baseline_expired"


def test_304_with_baseline_is_unchanged_without_is_new():
    result = engine_result(URL, "not_modified", http_status=304)
    classification, evidence = _classify(
        result, _context(baseline_request_id="req-1", baseline=_baseline_evidence())
    )
    assert (classification, evidence) == (
        UNCHANGED,
        {
            "why": "304_not_modified",
            "http_status": 304,
            "baseline_etag": ETAG_V1,
            "baseline_last_modified": None,
            "baseline_request_id": "req-1",
            "matched_validators": True,
        },
    )
    without = classify_result(result, context=_context(baseline_request_id="req-1"))
    assert without[0] == NEW


def _classified(
    result: EngineCrawlResult,
    *,
    baseline: dict[str, BaselineEvidence] | None = None,
    changed_since: float | None = None,
) -> tuple[str | None, dict[str, object]]:
    """Classify ``result`` under a per-caller baseline (or freshness-only) context."""
    context = _context(changed_since=changed_since, baseline_request_id="req-1", baseline=baseline or {})
    return _classify(result, context)


def test_200_against_validator_baseline_by_agreement():
    """Matching validators are unchanged, differing ones changed; no validators
    against a baseline that has one is a difference, never a silent unchanged."""
    cases = [
        ({"etag": ETAG_V1, "last_modified": "Wed"}, UNCHANGED, "validators_match"),
        ({"etag": '"v2"'}, CHANGED, "validators_diff"),
        ({}, CHANGED, "validators_diff"),
    ]
    for validators, expected, why in cases:
        result = engine_result(URL, "rendered", http_status=200, validators=validators)
        assert _classified(result, baseline=_baseline_evidence(last_modified="Wed"))[1]["why"] == why
        assert _classified(result, baseline=_baseline_evidence(last_modified="Wed"))[0] == expected


def test_200_against_fingerprint_baseline_by_agreement():
    """Fingerprints decide before validators: same body unchanged, other changed."""
    same = content_fingerprint(engine_result(URL, "rendered", markdown="same page"))
    baseline = _baseline_evidence(fingerprint=same)
    cases = [("same page", UNCHANGED, "same_fingerprint"), ("other page", CHANGED, "fingerprint_diff")]
    for body, expected, why in cases:
        result = engine_result(URL, "rendered", http_status=200, markdown=body)
        classification, evidence = _classified(result, baseline=baseline)
        assert (classification, evidence["why"]) == (expected, why)


def test_no_baseline_freshness_paths():
    """No baseline evidence: freshness decides, fail open toward change."""
    older = {"last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}
    newer = {"last-modified": "Thu, 02 Jan 2025 00:00:00 GMT"}
    cases: list[tuple[dict[str, str] | None, str]] = [
        (older, "last_modified_not_newer"),
        (newer, "last_modified_newer"),
        (None, "freshness_undeterminable"),
    ]
    expected_verdicts = [UNCHANGED, CHANGED, CHANGED]
    for (headers, why), expected in zip(cases, expected_verdicts, strict=True):
        result = engine_result(URL, "rendered", headers=headers or {})
        classification, evidence = _classified(result, changed_since=1_735_689_600.0)
        assert (classification, evidence["why"]) == (expected, why)


def test_changed_since_nothing_cached_is_new():
    result = engine_result(
        URL,
        "rendered",
        diagnostics={"retry": RetryDiagnostics(attempts=1, transient_errors=[], content_cache="miss")},
    )
    classification, evidence = _classified(result, changed_since=1_735_689_600.0)
    assert (classification, evidence["why"]) == (NEW, "nothing_cached")


def test_classification_is_baseline_scoped():
    """The same result is unchanged under its own baseline and new under an empty
    one -- the loader scopes the map, so the comparison cannot cross callers."""
    result = engine_result(URL, "rendered", http_status=200, validators={"etag": ETAG_V1})
    own = _context(baseline_request_id="req-a", baseline=_baseline_evidence())
    empty = _context(baseline_request_id="req-b", baseline={})
    assert classify_result(result, context=own)[0] == UNCHANGED
    assert classify_result(result, context=empty)[0] == NEW


def test_content_fingerprint_empty_is_none():
    for markdown in (None, "", "body"):
        value = content_fingerprint(engine_result(URL, "rendered", markdown=markdown))
        assert (value is None) == (not markdown)


def test_baseline_fingerprint_fields_record_what_was_hashed():
    """extract_baseline records which content fields fed the fingerprint, so a
    re-crawl at a different output shape compares over the same field set."""
    stored: dict[str, object] = {"results": [{"original_url": URL, "markdown": "body", "html": None}]}
    evidence = extract_baseline(stored)[URL]
    assert evidence.fingerprint_fields == ("markdown",)


def test_fingerprint_compared_over_baseline_field_set_not_whole_result():
    """The current result carries extra content fields the baseline stripped:
    the comparison hashes only the baseline's fields, so shape B vs shape A is
    unchanged when the shared fields match, and None-field incomparability
    degrades to validators, never a shifted-hash changed."""
    fingerprint = content_fingerprint(engine_result(URL, "rendered", markdown="body"))
    baseline = replace(_baseline_evidence(fingerprint=fingerprint)[URL], fingerprint_fields=("markdown",))
    evidence_dict = {URL: baseline}
    shape_b = engine_result(URL, "rendered", http_status=200, markdown="body", html="<p>body</p>")
    classification, evidence = _classified(shape_b, baseline=evidence_dict)
    assert (classification, evidence["why"]) == (UNCHANGED, "same_fingerprint")

    stripped = engine_result(URL, "rendered", http_status=200, validators={"etag": '"v2"'})
    fingerprint = content_fingerprint(engine_result(URL, "rendered", markdown="body"))
    missing = {URL: replace(baseline, fingerprint=fingerprint, fingerprint_fields=("markdown", "html"))}
    classification, evidence = _classified(stripped, baseline=missing)
    assert (classification, evidence["why"]) == (CHANGED, "validators_diff")


def test_removed_urls_for_delta():
    cases = [(["a", "b", "c"], ["a", "c"], ("b",)), ([], ["a"], ()), (["a"], [], ("a",))]
    for submitted, admitted, expected in cases:
        assert removed_urls_for(submitted, admitted) == expected


def _shaped(
    classification: str = "rendered", *, http_status: int = 200, markdown: str | None = None
) -> UnifiedCrawlResult:
    """A minimal shaped result carrying the fields ``delta_summary_for`` reads."""
    return UnifiedCrawlResult(
        original_url=URL,
        status="success",
        classification=classification,
        http_status=http_status,
        markdown=markdown,
    )


def test_delta_summary_none_when_not_requested():
    assert delta_summary_for([_shaped()]) is None
    assert delta_summary_for([], delta_requested=False) is None


def test_delta_summary_buckets_and_skipped():
    unchanged = _shaped("not_modified", http_status=304)
    unchanged.delta_classification = UNCHANGED
    changed = _shaped()
    changed.delta_classification = CHANGED
    fresh = _shaped()
    fresh.delta_classification = NEW
    summary = delta_summary_for([unchanged, changed, fresh], delta_requested=True)
    assert summary is not None
    assert summary["considered"] == 3
    assert summary["unchanged"] == summary["changed"] == summary["new"] == 1
    assert summary["removed"] == 0
    assert summary["skipped"] == [{"url": URL, "why": "unchanged"}]


def test_delta_summary_counts_removed():
    summary = delta_summary_for([], removed_urls=(URL,), delta_requested=True)
    assert summary == {
        "considered": 0,
        "new": 0,
        "changed": 0,
        "unchanged": 0,
        "removed": 1,
        "skipped": [{"url": URL, "why": "not_in_admitted_set"}],
    }


def test_delta_summary_unclassified_is_skipped_considered():
    summary = delta_summary_for([_shaped()], delta_requested=True)
    assert summary is not None
    assert summary["considered"] == 1
    assert summary["new"] == summary["changed"] == summary["unchanged"] == 0
    assert summary["skipped"] == [{"url": URL, "why": "classification_unavailable"}]


def test_extract_baseline_keys_normalized_and_original():
    stored: dict[str, object] = {
        "results": [
            {
                "original_url": URL.upper(),
                "normalized_url": URL,
                "validators": {"etag": ETAG_V1, "last_modified": "Wed"},
                "markdown": "body one",
            }
        ]
    }
    baseline = extract_baseline(stored)
    assert len(baseline) == 2
    evidence = baseline[URL]
    assert (evidence.etag, evidence.last_modified) == (ETAG_V1, "Wed")
    assert evidence.fingerprint is not None
    assert baseline[URL.upper()].fingerprint == evidence.fingerprint


def test_extract_baseline_ignores_garbage():
    assert extract_baseline({"results": "not a list"}) == {}
    assert extract_baseline({"results": [{"not_url": "x"}]}) == {}


def test_baseline_evidence_is_replaceable():
    evidence = BaselineEvidence(etag=ETAG_V1)
    replaced = replace(evidence, last_modified="Wed")
    assert (replaced.etag, replaced.last_modified, replaced.fingerprint) == (ETAG_V1, "Wed", None)
