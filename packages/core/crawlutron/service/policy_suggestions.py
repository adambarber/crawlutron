"""Candidate host policy rules, proposed from the recorded evidence crawls.

A read-only rollup over retained crawl records: blocked attempts per host propose
a deny entry with evidence. The REST router and the MCP tool are thin doors.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.classification_evidence import ClassificationEvidence
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.host_policy_suggestion import CandidateRule, HostPolicySuggestion
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import admit_or_none
from crawlutron.models.policy_suggestions_response import PolicySuggestionsResponse
from crawlutron.models.trace_response import TraceResponse
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.vocabulary.taxonomy import CrawlClassification

logger = logging.getLogger(__name__)

_EVIDENCE_SAMPLE = 5
"""How much evidence one suggestion carries.

Bounded because a host behind a challenge produces the same signature on every
attempt: without a bound the review surface grows with the crawl rather than
with the number of distinct findings, which is the opposite of what a reviewer
needs.
"""

_DENY_RULE: CandidateRule = "denied_hosts"

# The read bounds of one rollup request: pages keep peak memory at
# one bounded page, and the window caps the rows read so one GET is bounded work.
_PAGE_SIZE = 50
_MAX_RECORDS = 500
# Distinct signatures one host's suggestion reports: deduped in O(1) (never a
# list scan) and capped so the review surface stays a finding list, not a log.
_MAX_SIGNATURES = 32


def _host_of(url_text: str, *, host_policy: HostPolicy) -> str | None:
    """The registrable site of a stored URL, or None if this deployment refuses it.

    Read through the admission chokepoint (never a hand-rolled ``NormalizedUrl`` in a ``try``):
    refusing a URL is the constructor's call, and a second spelling diverges admission.
    """
    admitted = admit_or_none(url_text, policy=host_policy)
    return None if admitted is None else admitted.site


def build_policy_suggestions(
    records: list[TraceResponse],
    *,
    host_policy: HostPolicy,
    records_skipped: int = 0,
) -> PolicySuggestionsResponse:
    """Propose candidate rules from the blocked attempts in ``records``.

    Every attempt counts toward its host's observed total; only blocked ones
    count toward a candidate. ``records_skipped`` are the rows left unread.
    """
    blocked_attempts: dict[str, int] = {}
    observed_attempts: dict[str, int] = {}
    classifications: dict[str, set[CrawlClassification]] = {}
    signatures: dict[str, dict[str, None]] = {}
    evidence: dict[str, list[ClassificationEvidence]] = {}

    for record in records:
        for page in record.results:
            host = _host_of(page.normalized_url, host_policy=host_policy)
            if host is None:
                continue
            observed_attempts[host] = observed_attempts.get(host, 0) + 1
            if page.status != CrawlStatus.blocked:
                continue
            blocked_attempts[host] = blocked_attempts.get(host, 0) + 1
            classifications.setdefault(host, set()).add(page.classification)
            for item in page.classification_evidence:
                # dict-as-ordered-set: first-seen order on the wire, O(1) membership.
                seen = signatures.setdefault(host, {})
                if len(seen) < _MAX_SIGNATURES and item.signature not in seen:
                    seen[item.signature] = None
                sampled = evidence.setdefault(host, [])
                if len(sampled) < _EVIDENCE_SAMPLE:
                    sampled.append(item)

    suggestions = [
        HostPolicySuggestion(
            host=host,
            rule=_DENY_RULE,
            blocked_attempts=blocked_attempts[host],
            observed_attempts=observed_attempts[host],
            classifications=sorted(classifications[host]),
            signatures=list(signatures.get(host, {})),
            evidence=evidence.get(host, []),
            rationale=_rationale(
                host,
                blocked=blocked_attempts[host],
                observed=observed_attempts[host],
                classifications=sorted(classifications[host]),
            ),
        )
        for host in sorted(blocked_attempts)
    ]
    return PolicySuggestionsResponse(
        records_examined=len(records) + records_skipped,
        records_skipped=records_skipped,
        suggestions=suggestions,
    )


def _rationale(
    host: str,
    *,
    blocked: int,
    observed: int,
    classifications: list[CrawlClassification],
) -> str:
    return (
        f"{blocked} of {observed} retained attempts on {host} were blocked "
        f"({', '.join(classifications)}). Candidate rule: add {host} to denied_hosts. "
        f"Proposed for review only; this service never applies it."
    )


def _valid_traces(payloads: list[JsonValue]) -> tuple[list[TraceResponse], int]:
    """The retained payloads that parse as traces, one row at a time, and how many did not.

    A single malformed retained row must not 500 the whole rollup: it is skipped
    and reported, matching the per-row isolation in ``job_store``.
    """
    traces: list[TraceResponse] = []
    skipped = 0
    for payload in payloads:
        try:
            traces.append(TraceResponse.model_validate(payload))
        except ValidationError as exc:
            skipped += 1
            logger.warning(
                "policy_suggestions_skip_malformed",
                extra={"error": str(exc), "request_id": _request_id_of(payload)},
            )
    return traces, skipped


def _request_id_of(payload: JsonValue) -> str | None:
    """The retained row's own id, so a skip names the row it skipped."""
    if isinstance(payload, dict):
        value = payload.get("request_id")
        return value if isinstance(value, str) else None
    return None


async def policy_suggestions(
    result_store: CrawlResultStore,
    settings: CrawlerSettings,
    *,
    owner: str | None = None,
) -> PolicySuggestionsResponse:
    """Propose candidate host rules from a bounded window of retained records."""
    host_policy = HostPolicy.from_settings(settings)
    records: list[TraceResponse] = []
    skipped = 0
    read = 0
    cursor: tuple[float, str] | None = None
    while read < _MAX_RECORDS:
        page = await result_store.apayloads_page(
            limit=min(_PAGE_SIZE, _MAX_RECORDS - read), cursor=cursor, owner=owner
        )
        if not page:
            break
        cursor = page[-1][0]
        read += len(page)
        # Validation is per-row pydantic work; running it (and the rollup below)
        # on the loop is what stalled every concurrent request.
        page_records, page_skipped = await asyncio.to_thread(_valid_traces, [payload for _, payload in page])
        records.extend(page_records)
        skipped += page_skipped
    if read >= _MAX_RECORDS:
        # Window full: count the rows it did not reach instead of guessing, so
        # records_examined stays honest (rows expiring mid-scan clamp at zero).
        skipped += max(await result_store.aretained_count(owner=owner) - read, 0)
    return await asyncio.to_thread(
        build_policy_suggestions, records, host_policy=host_policy, records_skipped=skipped
    )


__all__ = ["build_policy_suggestions", "policy_suggestions"]
