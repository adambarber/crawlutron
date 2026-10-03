"""A cgroup budget is shared; the pools that spend it are not.

`derive_browser_pool_size` and `max_http_concurrency_for` size themselves from
the cgroup memory limit. That limit belongs to the container, so it is the
same number in every worker -- but each worker's pools are its own. With N
workers every one of them reads the whole budget and grants a whole pool's
worth, so the fleet spends N times the RAM the admission gate is reasoning
about. The gate reads genuinely shared pressure and then hands out per-process
capacity at the observed ratio, which is the combination that produces an OOM
kill of the whole service rather than a slow request.

The division belongs where the budget is derived, not at each pool's call site:
the httpx pool, the browser pool and the extraction pool all size from these
functions, and dividing at a call site would let one of them keep the
undivided number.

`doc_worker_count` is in the same file for the same reason -- each persistent
markitdown worker holds 100-300 MiB for the life of the process, so N workers
at the default 2 is 2N of them.
"""

import pytest

from app import resource_pressure as rp


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

    The assertion is on the fleet total against the cap that names it, not
    against a single worker's pool: with N workers each guaranteed at least one
    browser, an undersized container cannot always keep the fleet at or below
    what one worker would have taken, and pretending otherwise would only be
    satisfiable by giving a worker no browser at all.
    """
    limit = 16 * 1024 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "")
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
    per_worker = rp.derive_browser_pool_size(limit)

    assert per_worker * 4 <= rp._MAX_DERIVED_POOL_SIZE
    assert per_worker >= 1

    # Un-divided, 4 workers on this limit would each take the full cap.
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert rp.derive_browser_pool_size(limit) * 4 > per_worker * 4


def test_the_pool_size_knob_is_a_fleet_total_not_a_per_process_one(monkeypatch):
    """An operator setting 4 means four browsers, not four per worker.

    With the shipped single-worker configuration the two readings coincide, so
    this changes no shipped behaviour -- it stops the fleet from quietly
    multiplying the number the operator thought they were capping.
    """
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


def test_doc_workers_are_split_across_workers(monkeypatch):
    """2 markitdown workers per process is 2N of them, each 100-300 MiB."""
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "8")
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert rp.doc_worker_count() == 8

    monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
    assert rp.doc_worker_count() == 2


def test_the_content_length_budget_is_split_across_workers(monkeypatch):
    """The last cgroup budget that was read undivided.

    ``CRAWLUTRON_CONTENT_RAM_BUDGET_BYTES`` bounds the RAM held by in-flight
    page bodies, which is container memory -- the same shared number the pool
    sizes read. Left whole, each worker sized its per-page cap against the whole
    budget, so the fleet held N times what the knob names. This is the same
    over-admission ``test_http_concurrency_is_split_across_workers`` covers for
    connections, reached through the one budget not in that sweep.

    The two clamps on this cap are different kinds of quantity and the test keeps
    them apart. ``DEFAULT_MAX_CONTENT_LENGTH_BYTES`` is a per-page ceiling, so it
    is not divided and is what binds when the budget is generous; the RAM budget
    is a fleet aggregate, so it is divided and is what binds when it is not.
    """
    from app.crawl.strategies import content_cache_key as cck
    from app.models.crawl_policy_limits import CrawlPolicyLimits

    budget = 16 * 1024 * 1024
    monkeypatch.setattr(cck, "_RAM_SAFETY_BYTES", budget)
    uncapped = CrawlPolicyLimits()

    # 16 MiB across one worker clears the 10 MiB per-page default, so the
    # default binds and is reported undivided.
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert cck._effective_max_content_length(uncapped, global_concurrency=1) == 10 * 1024 * 1024

    # Below the default the budget is the binding constraint, and each worker
    # takes its share of it.
    for workers in (2, 4, 8):
        monkeypatch.setenv("CRAWLUTRON_WORKERS", str(workers))
        each = cck._effective_max_content_length(uncapped, global_concurrency=1)
        assert each == budget // workers, f"at {workers} workers"


def test_an_explicit_content_cap_is_not_divided(monkeypatch):
    """A per-page cap the caller set is that caller's business, not the fleet's.

    Division answers "how much of a SHARED budget may this process hold", and an
    explicit cap is a single page the caller already bounded. Scaling it by the
    worker count would silently shrink a policy the operator wrote on purpose.
    """
    from app.crawl.strategies import content_cache_key as cck
    from app.models.crawl_policy_limits import CrawlPolicyLimits

    explicit = CrawlPolicyLimits(max_content_length_bytes=5 * 1024 * 1024)

    monkeypatch.setenv("CRAWLUTRON_WORKERS", "1")
    assert cck._effective_max_content_length(explicit, global_concurrency=1) == 5 * 1024 * 1024
    monkeypatch.setenv("CRAWLUTRON_WORKERS", "8")
    assert cck._effective_max_content_length(explicit, global_concurrency=1) == 5 * 1024 * 1024


def test_a_worker_always_gets_at_least_one_slot(monkeypatch):
    """Division must never produce a zero budget.

    A worker that admits nothing is indistinguishable from a hung service, and
    an over-subscribed fleet is the operator's memory-limit error, not a
    reason to make the crawler refuse to work at all.
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
    """The third defect, and the reason no distributed semaphore is needed.

    The gate reads cgroup pressure, which every worker shares, and grants
    capacity per process. N uncoordinated controllers therefore admit N times
    what the measured pressure supports. The obvious fix is a shared semaphore,
    but a semaphore has to lease and expire slots or a dead worker's share is
    lost forever, and it has to be contended on every batch.

    Dividing the ceiling is strictly better here: the resource is a shared,
    slow-moving budget rather than a contended instantaneous semaphore, so a
    static split needs no coordination, cannot leak, and cannot over-admit. The
    price is that an idle worker's share goes unused -- under-utilisation,
    which is the safe direction to be wrong in.
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

    Every budget is floored at one so a worker is never useless, and one
    browser is 700 MiB reserved. That is the right trade -- under-utilisation
    beats a worker that cannot render anything -- but it is only free while N
    browsers fit the budget the pool sizing assumed. Past that point the fleet
    is over-committed by exactly the overflow, and the operator's remedy is
    fewer workers or more RAM, not a different setting.

    Naming the crossing point is the point: it is the number a deployment has
    to stay under, and an undocumented floor is how a container gets OOM-killed
    by a config that looks conservative everywhere.
    """
    limit = 16 * 1024 * 1024 * 1024
    budget = int(limit * rp.BROWSER_RAM_FRACTION)
    browser_bytes = rp._BROWSER_TASK_RAM_BYTES

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
