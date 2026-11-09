"""A cgroup budget is shared; the pools that spend it are not.

Every worker grants a whole pool's worth, so N workers spend N times the RAM,
so the division belongs where the budget is DERIVED (hence ``doc_worker_count``).
"""

import pytest

from crawlutron.runtime import pressure_sizing as rp


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch):
    """Default every test to a single worker unless it says otherwise."""
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")


def test_worker_count_reads_through_the_never_raise_chokepoint(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")

    assert rp.worker_count() == 4


def test_a_typo_in_the_worker_count_does_not_zero_every_budget(monkeypatch):
    """A malformed value must fall back to one worker, not to zero.

    Zero would divide every budget to nothing and silently refuse all work,
    which reads as "the crawler is broken" rather than "the config is wrong".
    """
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "many")

    assert rp.worker_count() == 1


def test_a_non_positive_worker_count_is_clamped_to_one(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "0")

    assert rp.worker_count() == 1


def test_every_worker_derives_a_pool_that_fits_the_whole_fleet(monkeypatch):
    """The defect: N workers each spending the whole RAM budget.

    The assertion is the fleet total against the cap that names it; an
    undersized container cannot keep the fleet at what one worker would take.
    """
    limit = 16 * 1024 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "")
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
    per_worker = rp.derive_browser_pool_size(limit)

    assert per_worker * 4 <= rp.MAX_DERIVED_POOL_SIZE
    assert per_worker >= 1

    # Un-divided, 4 workers on this limit would each take the full cap.
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert rp.derive_browser_pool_size(limit) * 4 > per_worker * 4


def test_the_pool_size_knob_is_a_fleet_total_not_a_per_process_one(monkeypatch):
    """An operator setting 4 means four browsers, not four per worker.

    The shipped single-worker config makes the readings coincide, so this
    changes no shipped behaviour -- it stops a quiet fleet-wide multiply."""
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "4")
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert rp.derive_browser_pool_size() == 4

    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
    assert rp.derive_browser_pool_size() == 1


def test_http_concurrency_is_split_across_workers(monkeypatch):
    """Each worker's HTTP ceiling is its share of the container's RAM."""
    limit = 4 * 1024 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    single = rp.max_http_concurrency_for(limit, pool_size=1, doc_workers=1)

    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
    per_worker = rp.max_http_concurrency_for(limit, pool_size=1, doc_workers=1)

    assert per_worker * 4 <= single
    assert per_worker >= 1


def test_doc_worker_count_is_a_per_process_knob_whose_fleet_total_is_exact(monkeypatch):
    """The knob is per process: the fleet spends knob x workers, exactly.

    The old floor-division made the fleet total drift: 2 configured across 3
    workers spawned 3 pools (2 // 3 floored to 1, x3 workers), and 3 across 2
    workers under-delivered the operator's knob (#666). The honest derivation
    reads the knob per process; the fleet total is knob x worker_count().
    """
    for knob, workers in ((2, 3), (3, 2), (2, 1), (5, 4)):
        monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", str(knob))
        monkeypatch.setenv("CRAWLUTRON_WORKERS", str(workers))
        assert rp.doc_worker_count() == knob, (knob, workers)
        assert rp.doc_worker_count() * rp.worker_count() == knob * workers, (knob, workers)


def test_doc_worker_count_defaults_to_two_per_process(monkeypatch):
    """The shipped single-worker deployment runs the 2-worker default.

    CRAWLUTRON_WORKERS is pinned to 1 in compose, so the default knob is both
    the per-process count and the fleet total there.
    """
    monkeypatch.delenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", raising=False)
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")

    assert rp.doc_worker_count() == 2


def test_the_content_length_budget_is_split_across_workers(monkeypatch):
    """The last cgroup budget read undivided.

    Left whole, the fleet held N times the ``CONTENT_RAM`` knob; the per-page
    ceiling is NOT divided (binds when generous), the fleet RAM aggregate IS.
    """
    from crawlutron.models.crawl_policy_limits import CrawlPolicyLimits
    from crawlutron.transport import content_cache_key as cck

    budget = 16 * 1024 * 1024
    monkeypatch.setattr(cck, "content_ram_budget_bytes", lambda: budget)
    uncapped = CrawlPolicyLimits()

    # 16 MiB across one worker clears the 10 MiB per-page default, so the default binds and is reported
    # undivided.
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert cck.effective_max_content_length(uncapped, global_concurrency=1) == 10 * 1024 * 1024

    # Below the default the budget is the binding constraint, and each worker takes its share of it.
    for workers in (2, 4, 8):
        monkeypatch.setenv("CRAWLUTRON_WORKERS", str(workers))
        each = cck.effective_max_content_length(uncapped, global_concurrency=1)
        assert each == budget // workers, f"at {workers} workers"


def test_an_explicit_content_cap_is_not_divided(monkeypatch):
    """A per-page cap the caller set is that caller's business, not the fleet's.

    Division answers "how much SHARED budget may this process hold"; an explicit
    cap is single-page, and scaling it shrinks an operator-written policy."""
    from crawlutron.models.crawl_policy_limits import CrawlPolicyLimits
    from crawlutron.transport import content_cache_key as cck

    explicit = CrawlPolicyLimits(max_content_length_bytes=5 * 1024 * 1024)

    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert cck.effective_max_content_length(explicit, global_concurrency=1) == 5 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "8")
    assert cck.effective_max_content_length(explicit, global_concurrency=1) == 5 * 1024 * 1024


def test_a_worker_always_gets_at_least_one_slot(monkeypatch):
    """Division must never produce a zero budget.

    A worker that admits nothing is indistinguishable from a hung service; an
    over-subscribed fleet is the operator's memory error, not a reason to refuse.
    """
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "64")

    assert rp.derive_browser_pool_size(64 * 1024 * 1024) >= 1
    assert rp.max_http_concurrency_for(64 * 1024 * 1024, pool_size=1) >= 1
    assert rp.doc_worker_count() >= 1


def _policy(limit: int) -> rp.AdmissionPolicy:
    return rp.build_admission_policy(
        limit,
        pool_size=rp.derive_browser_pool_size(limit),
        max_concurrent_pages=3,
        budget=rp.TaskRamBudget(doc_workers=rp.doc_worker_count()),
    )


def test_the_admission_gate_grants_the_fleet_no_more_than_one_worker_would(monkeypatch):
    """The defect, and why no distributed semaphore is needed.

    N uncoordinated controllers reading shared pressure admit N times it; a
    way static SPLIT of a slow budget cannot leak or over-admit.
    """
    limit = 16 * 1024 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    single = _policy(limit)

    for workers in (2, 4, 8):
        monkeypatch.setenv("CRAWLUTRON_WORKERS", str(workers))
        each = _policy(limit)
        assert each.max_http_concurrency * workers <= single.max_http_concurrency, (
            f"http fleet at {workers} workers"
        )


def test_the_browser_tier_stays_inside_the_ram_budget(monkeypatch):
    """The bound the floor of one browser per worker puts on the worker count.

    One browser is 700 MiB, so the floor is free only while N fit the sized
    budget; an undocumented floor is how a container gets OOM-killed.
    """
    limit = 16 * 1024 * 1024 * 1024
    budget = int(limit * rp.BROWSER_RAM_FRACTION)
    browser_bytes = rp.TaskRamBudget().browser_task_bytes

    affordable = budget // browser_bytes
    for workers in range(1, affordable + 1):
        monkeypatch.setenv("CRAWLUTRON_WORKERS", str(workers))
        assert rp.derive_browser_pool_size(limit) * workers * browser_bytes <= budget, (
            f"over-committed at {workers} workers"
        )

    # One past the ceiling is over-committed, and that is the documented limit.
    monkeypatch.setenv("CRAWLUTRON_WORKERS", str(affordable + 1))
    over = rp.derive_browser_pool_size(limit) * (affordable + 1) * browser_bytes
    assert over > budget


def test_the_content_ram_budget_is_read_at_call_time_not_import_time(monkeypatch):
    """A frozen-at-import budget is indistinguishable from a derived one once wrong:
    the only evidence is that a later env change takes effect, so the knob is
    re-read per call through the declared derivation, never a module snapshot.
    """
    from crawlutron.transport import content_cache_key as cck

    monkeypatch.delenv("CRAWLUTRON_CONTENT_RAM_BUDGET_BYTES", raising=False)
    assert cck.content_ram_budget_bytes() == 1 * 1024 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_CONTENT_RAM_BUDGET_BYTES", "4194304")
    assert cck.content_ram_budget_bytes() == 4 * 1024 * 1024
