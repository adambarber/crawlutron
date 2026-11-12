"""Behavioral proofs: agent-hints advertise every enum member (issue #139)."""

import pytest

from crawlutron.discovery.agent_hints import agent_hints_payload
from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.vocabulary.extraction import EXTRACTION_DESCRIPTIONS


def test_hints_advertise_every_extraction_method():
    """Adding an extraction member without a hint is a drift the gate refuses."""
    payload = agent_hints_payload()
    missing = {m.value for m in CrawlExtractionMethod} - set(payload["extractions"])
    assert not missing, f"agent hints do not advertise extraction methods: {sorted(missing)}"


def test_hints_advertise_every_strategy():
    """Same completeness law for strategies."""
    payload = agent_hints_payload()
    missing = {s.value for s in CrawlStrategy} - set(payload["strategies"])
    assert not missing, f"agent hints do not advertise strategies: {sorted(missing)}"


def test_extraction_descriptions_come_from_the_vocabulary():
    """The payload reuses the vocabulary's descriptions verbatim: one table, no mirror."""
    payload = agent_hints_payload()
    for key, description in EXTRACTION_DESCRIPTIONS.items():
        assert payload["extractions"].get(key) == description


@pytest.mark.parametrize(
    "method",
    [m for m in CrawlExtractionMethod],
    ids=[m.value for m in CrawlExtractionMethod],
)
def test_each_extraction_method_is_described_in_the_vocabulary(method):
    """The vocabulary table itself stays complete; this bites before hints do."""
    assert method.value in EXTRACTION_DESCRIPTIONS


def test_hints_disclose_per_request_scope_of_domain_limits():
    """The machine-readable hints state that per-domain limits are per request.

    Without this disclosure a caller reading `per_domain_concurrency` could believe a service-wide bound;
    two concurrent requests each enforce independently (#481).
    """
    guidance = " ".join(agent_hints_payload()["guidance"]).lower()
    assert "within one request" in guidance
    assert "each request runs its own scheduler" in guidance
