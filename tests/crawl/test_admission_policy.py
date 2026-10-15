"""Page budgets, policy derivation and requeue overcommit fail closed."""

import asyncio
from types import SimpleNamespace

import pytest

from app.admission.admission import requeue_admitted_pages
from app.admission.payload_helpers import payload_policy
from app.admission.policy import PolicyTier
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.runtime.resource_pressure import (
    BROWSER_RAM_FRACTION,
    ResourceGate,
    TaskRamBudget,
    WeightedAdmission,
    build_admission_policy,
    default_admission_policy,
)
from app.storage.job_store import FrontierEntry
from tests._fetcher_factories import crawled_results
from tests._policy_factories import admit, canonical
from tests.crawl.test_admission_scaling import PinnedGate


async def test_max_pages_default_is_bounded_not_unlimited():
    policy = payload_policy({"url": "http://example.com/", "policy": {}}, PolicyTier.model_defaults())
    # DEFAULT_MAX_PAGES, not 0/"unlimited".
    assert policy.limits.max_pages == 100


async def test_max_pages_zero_falls_back_to_default():
    policy = payload_policy(
        {"url": "http://example.com/", "policy": {"limits": {"max_pages": 0}}}, PolicyTier.model_defaults()
    )
    assert policy.limits.max_pages == 100


async def test_max_pages_above_upper_bound_is_rejected_not_clamped():
    # The model declares le=2000.
    with pytest.raises(ValueError, match="invalid_policy_limits"):
        payload_policy(
            {"url": "http://example.com/", "policy": {"limits": {"max_pages": 99999}}},
            PolicyTier.model_defaults(),
        )


async def test_max_pages_positive_preserved():
    policy = payload_policy(
        {"url": "http://example.com/", "policy": {"limits": {"max_pages": 7}}}, PolicyTier.model_defaults()
    )
    assert policy.limits.max_pages == 7


async def test_max_pages_zero_bounded_at_engine():
    # Wide fixture: root links to 150 leaf pages. With max_pages=0 the crawl must be bounded
    # (DEFAULT_MAX_PAGES=100), not crawl all 151 reachable pages.
    pages = {
        "/": (200, "<html>" + "".join(f'<a href="/c{i}">c</a>' for i in range(150)) + "</html>", "text/html"),
    }
    for i in range(150):
        pages[f"/c{i}"] = (200, f"<html>child {i}</html>", "text/html")
    results = (await crawled_results(pages, limits={"max_depth": 1, "max_pages": 0}))["results"]
    assert 1 < len(results) <= 100
    # Without the bound it would have crawled root + 150 children = 151.
    assert len(results) < 151


def _policy(limit=3 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3, doc_workers=2):
    return build_admission_policy(
        limit,
        pool_size=pool_size,
        max_concurrent_pages=max_concurrent_pages,
        budget=TaskRamBudget(doc_workers=doc_workers),
    )


def _unreserved_bytes(policy) -> int:
    """The 3 GiB limit minus the reserves that are already spent.

    Both reserves are RAM a consumer holds for the life of the process rather
    than RAM a request borrows, so the HTTP ceiling is sized from what is left.
    One function because two tests assert the same subtraction and a third
    that got it wrong would be invisible: the arithmetic IS the contract.
    """
    return (3 * 1024 * 1024 * 1024) - policy.browser_pool_reserve_bytes - policy.doc_pool_reserve_bytes


class TestPolicyDerivation:
    def test_browser_pool_reserve_subtracted_from_limit(self):
        # 3 GiB limit, 2 browsers * 700 MiB reserve and the default doc pool, -> HTTP ceiling from the
        # remainder.
        p = _policy()
        weights = TaskRamBudget()
        assert p.browser_pool_reserve_bytes == 2 * weights.browser_task_bytes
        assert p.doc_pool_reserve_bytes == 2 * weights.doc_task_bytes
        assert p.max_http_concurrency == max(1, _unreserved_bytes(p) // weights.http_task_bytes)
        # Browser tier is capped at pool_size * max_concurrent_pages
        assert p.max_browser_concurrency == 2 * 3

    def test_http_ceiling_capped_at_hard_max(self):
        huge = build_admission_policy(1024 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3)
        assert huge.max_http_concurrency <= 512

    def test_smaller_limit_yields_smaller_http_ceiling(self):
        small = _policy(limit=1024 * 1024 * 1024)  # 1 GiB
        big = _policy(limit=3 * 1024 * 1024 * 1024)
        assert small.max_http_concurrency < big.max_http_concurrency

    def test_doc_pool_reserve_subtracted_from_limit(self):
        # The persistent markitdown workers are a fixed RAM consumer exactly like the browser pool: each
        # imports markitdown + the PDF/Office parsers once and stays alive.
        p = _policy(doc_workers=2)
        assert p.doc_pool_reserve_bytes == 2 * TaskRamBudget().doc_task_bytes
        assert p.max_http_concurrency == max(1, _unreserved_bytes(p) // TaskRamBudget().http_task_bytes)

    def test_doc_pool_reserve_lowers_the_http_ceiling(self):
        # The reserve must actually bite: admitting with doc workers must admit strictly fewer concurrent HTTP
        # tasks than without them.
        without = _policy(doc_workers=0)
        with_docs = _policy(doc_workers=2)
        assert with_docs.max_http_concurrency < without.max_http_concurrency

    def test_default_policy_reserves_the_default_doc_pool(self):
        # Default_admission_policy is the conservative fallback (3 GiB box); it must reserve the default doc
        # worker count, not zero.
        gate = ResourceGate()
        wa = default_admission_policy(gate)
        assert wa._p.doc_pool_reserve_bytes == 2 * TaskRamBudget().doc_task_bytes


class TestWeightedAdmission:
    def test_headroom_from_most_contended_resource(self):
        gate = PinnedGate(mem=0.3, cpu=0.8)
        a = WeightedAdmission(gate, _policy())
        # Min(1-0.3, 1-0.8) = 0.2
        assert a.headroom_fraction() == pytest.approx(0.2)

    def test_http_capacity_scales_with_headroom(self):
        gate = PinnedGate(mem=0.0, cpu=0.0)
        a = WeightedAdmission(gate, _policy())
        assert a.http_capacity() == a._p.max_http_concurrency
        gate2 = PinnedGate(mem=0.5, cpu=0.5)
        a2 = WeightedAdmission(gate2, _policy())
        assert a2.http_capacity() == pytest.approx(a._p.max_http_concurrency * 0.5, rel=0.01)

    async def test_admit_grants_all_when_headroom_full(self):
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=0.0), _policy())
        # Request exactly the tier ceiling so the claim ("grants everything asked while headroom is full")
        # tracks the sizing default, whatever it is.
        h, b = await a.admit(want_http=a._p.max_http_concurrency, want_browser=2)
        assert h == a._p.max_http_concurrency
        assert b == 2

    async def test_admit_caps_browser_tier(self):
        # Want more browsers than the tier allows; HTTP still granted.
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=0.0), _policy())
        h, b = await a.admit(want_http=10, want_browser=99)
        assert b == a.browser_capacity()  # capped
        assert h == 10

    async def test_admit_shrinks_http_under_memory_pressure(self):
        a = WeightedAdmission(PinnedGate(mem=0.9, cpu=0.0), _policy())
        h, b = await a.admit(want_http=200, want_browser=0)
        # Headroom 0.1 -> http capacity ~10% of ceiling
        assert h < 200
        assert h > 0

    async def test_admit_yields_briefly_when_fully_saturated(self):
        # Saturated (headroom 0) with demand -> returns (0,0) after a short yield instead of parking the whole
        # crawl indefinitely.
        a = WeightedAdmission(PinnedGate(mem=1.0, cpu=1.0), _policy())
        t0 = asyncio.get_event_loop().time()
        h, b = await a.admit(want_http=5, want_browser=1)
        elapsed = asyncio.get_event_loop().time() - t0
        assert (h, b) == (0, 0)
        assert 0 < elapsed < 1.0  # yielded, did not block forever


class TestBrowserTierDoesNotDoubleScale:
    """The browser pool's RAM is reserved up front, so the tier must not
    shrink against its own reserved footprint — only usage beyond the fraction
    the pool sizing already assumed should yield browser capacity."""

    def test_full_capacity_while_within_the_reserved_fraction(self):
        # Browsers using exactly the fraction derive_browser_pool_size assumed is the steady state, not
        # pressure.
        a = WeightedAdmission(PinnedGate(mem=BROWSER_RAM_FRACTION, cpu=0.0), _policy())
        assert a.browser_capacity() == a._p.max_browser_concurrency

    def test_decays_only_for_usage_beyond_the_reserved_fraction(self):
        a = WeightedAdmission(PinnedGate(mem=BROWSER_RAM_FRACTION + 0.2, cpu=0.0), _policy())
        # 20 points of surplus usage out of a 50-point ramp to exhaustion -> 60% of the tier still admits.
        assert a.browser_capacity() == pytest.approx(a._p.max_browser_concurrency * 0.6, abs=1)

    def test_never_admits_when_saturated(self):
        a = WeightedAdmission(PinnedGate(mem=1.0, cpu=0.0), _policy())
        assert a.browser_capacity() == 0

    def test_cpu_saturation_still_shrinks_the_browser_tier(self):
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=1.0), _policy())
        assert a.browser_capacity() == 0


class TestHTTPTaskRamEstimate:
    """The per-HTTP-task estimate must cover a real task — a max-length
    body plus the trafilatura/lxml parse state — or admission over-grants and
    the box swaps. 12 MiB measured optimistic at the review's live scale."""

    def test_default_covers_a_max_body_plus_parse_state(self):
        # A 10 MiB body plus lxml/trafilatura working set is 40+ MiB in practice.
        assert TaskRamBudget().http_task_bytes >= 48 * 1024 * 1024

    def test_http_ceiling_uses_the_same_bytes_as_admission(self):
        # Zero drift: the pool ceiling and the admission policy divide by the same per-task estimate, whatever
        # the default is.
        p = _policy()
        free = (3 * 1024 * 1024 * 1024) - p.browser_pool_reserve_bytes - p.doc_pool_reserve_bytes
        assert p.max_http_concurrency == max(1, free // TaskRamBudget().http_task_bytes)

    def test_the_3gib_default_still_admits_a_working_floor(self):
        # The minimal-deployment default (3 GiB, 2 browsers) must still admit enough concurrent HTTP fetches
        # for hundreds of pages per minute.
        p = _policy()
        assert p.max_http_concurrency >= 16


async def _allowed() -> bool:
    return True


async def _no_crawl_delay(url: str) -> float | None:
    """A host that states no Crawl-delay, so a scenario about capacity can pass one."""
    return None


class _RequeueJob:
    """The slice of a job state that the requeue path actually reads.

    Deliberately narrow: this names the contract ``requeue_admitted_pages``
    depends on, so a change that widens what it reads shows up as a missing
    attribute here rather than as a confusing failure inside a real job row.
    """

    def __init__(self, *, policy, tier=None, visited=()):
        self.policy = policy
        self.tier = tier
        self.visited = set(visited)
        self.in_flight = []
        self.frontier = []
        self.stats: dict[str, int] = {}
        self.job_id = "job-requeue"


def _requeue_context():
    return SimpleNamespace(host_policy=HostPolicy.unrestricted())


class TestRequeueOvercommitContract:
    """``cap_blocked`` and ``cap_overcommitted`` must mean opposite things.

    Both arrive as a number in an ``AdmissionOutcome``, and on the requeue path
    the count was reported as ``cap_blocked`` -- "the cap refused this" -- for
    entries that were admitted anyway. A reader reconciling the outcome against
    the frontier would find one candidate that was simultaneously refused and
    present, and the two facts are not reconcilable.
    """

    def test_requeue_past_the_cap_admits_and_reports_overcommit_not_blocked(self):
        job = _RequeueJob(policy={"limits": {"max_pages": 2}}, visited=["https://a.test/", "https://b.test/"])

        outcome = requeue_admitted_pages(
            job,
            [FrontierEntry(url=canonical("https://c.test/"), depth=1)],
            _requeue_context(),
        )

        assert [entry.url.raw for entry in job.frontier] == ["https://c.test"]
        assert outcome.accepted == 1
        assert outcome.cap_overcommitted == 1
        assert outcome.cap_blocked is False, "admitting past the cap is not a refusal"
        assert job.stats["cap_overcommitted"] == 1

    def test_requeue_under_the_cap_is_plain_admission_with_no_cap_signal(self):
        job = _RequeueJob(policy={"limits": {"max_pages": 2}}, visited=["https://a.test/"])

        outcome = requeue_admitted_pages(
            job,
            [FrontierEntry(url=canonical("https://b.test/"), depth=1)],
            _requeue_context(),
        )

        assert outcome.accepted == 1
        assert outcome.cap_blocked is False
        assert outcome.cap_overcommitted == 0
        assert "cap_overcommitted" not in job.stats
        assert "cap_truncated" not in job.stats

    async def test_normal_admission_refuses_past_the_cap_and_flags_no_overcommit(self):
        """The ordinary path refuses, and the two signals never both fire.

        Together with the two tests above this is the whole contract: refusal
        and overcommit are opposites, each belongs to exactly one path, and a
        caller can tell them apart by which number is non-zero.
        """
        frontier: list[NormalizedUrl] = []
        outcome = await admit(
            ["https://example.test/one", "https://example.test/two"],
            frontier=frontier,
            max_pages=1,
            robots_allows=lambda url: _allowed(),
            robots_crawl_delay=_no_crawl_delay,
            commit_crawl_delay=lambda domain, delay: None,
        )

        assert outcome.cap_blocked is True, "the cap refused the second link"
        assert outcome.cap_overcommitted == 0, "a refusal is not an overcommit"
        assert [url.raw for url in frontier] == ["https://example.test/one"]
