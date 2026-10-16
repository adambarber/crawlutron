"""Candidate host policy rules, proposed from the recorded evidence crawls.

A read-only rollup over retained crawl records: blocked attempts per host propose
a deny entry with evidence; policy is read only to get a host off a stored URL.
"""

from __future__ import annotations

import logging

from app.api.dependencies import ResultStoreDep, SettingsDep
from app.models.classification_evidence import ClassificationEvidence
from app.models.crawl_status import CrawlStatus
from app.models.host_policy import HostPolicy
from app.models.host_policy_suggestion import CandidateRule, HostPolicySuggestion
from app.models.normalized_url import admit_or_none
from app.models.policy_suggestions_response import PolicySuggestionsResponse
from app.models.trace_response import TraceResponse
from app.vocabulary.taxonomy import CrawlClassification

logger = logging.getLogger(__name__)

_EVIDENCE_SAMPLE = 5
"""How much evidence one suggestion carries.

Bounded because a host behind a challenge produces the same signature on every
attempt: without a bound the review surface grows with the crawl rather than
with the number of distinct findings, which is the opposite of what a reviewer
needs.
"""

_DENY_RULE: CandidateRule = "denied_hosts"


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
) -> PolicySuggestionsResponse:
    """Propose candidate rules from the blocked attempts in ``records``.

    Every attempt counts toward its host's observed total; only blocked ones count toward
    a candidate, and an evidence-less block still counts (contributing none, not dropped).
    """
    blocked_attempts: dict[str, int] = {}
    observed_attempts: dict[str, int] = {}
    classifications: dict[str, set[CrawlClassification]] = {}
    signatures: dict[str, list[str]] = {}
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
                seen = signatures.setdefault(host, [])
                if item.signature not in seen:
                    seen.append(item.signature)
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
            signatures=signatures.get(host, []),
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
    return PolicySuggestionsResponse(records_examined=len(records), suggestions=suggestions)


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


async def policy_suggestions(
    result_store: ResultStoreDep,
    settings: SettingsDep,
) -> PolicySuggestionsResponse:
    """Propose candidate host rules from every retained crawl record."""
    records = [TraceResponse.model_validate(payload) for payload in await result_store.apayloads()]
    return build_policy_suggestions(records, host_policy=HostPolicy.from_settings(settings))


__all__ = ["build_policy_suggestions", "policy_suggestions"]
