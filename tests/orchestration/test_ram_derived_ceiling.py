"""The scheduler's RAM-derived HTTP ceiling matches admission's (#367)."""

from app.orchestration.request_pipeline import compute_ram_derived_concurrency
from app.runtime.memory_pressure import read_memory_stats
from app.runtime.resource_pressure import (
    TaskRamBudget,
    build_admission_policy,
    doc_worker_count,
    max_http_concurrency_for,
)
from app.transport.browser_pool import derive_browser_pool_size


def test_ram_derived_scheduler_ceiling_accounts_for_doc_workers():
    """The scheduler's RAM-derived ceiling reserves the doc pool too (#367):
    the same limit yields the same ceiling for the scheduler and for weighted
    admission, which passes doc_workers=doc_worker_count()."""
    limit = read_memory_stats().total_bytes or 0
    expected = max(
        1,
        max_http_concurrency_for(limit, derive_browser_pool_size(), doc_workers=doc_worker_count()),
    )
    assert compute_ram_derived_concurrency(None) == expected


def test_ram_derived_ceiling_matches_admission_policy():
    """End to end (#367): the scheduler ceiling equals admission's max_http_concurrency."""
    limit = read_memory_stats().total_bytes or 0
    admission = build_admission_policy(
        limit,
        pool_size=derive_browser_pool_size(),
        max_concurrent_pages=3,
        budget=TaskRamBudget(doc_workers=doc_worker_count()),
    )
    assert compute_ram_derived_concurrency(None) == admission.max_http_concurrency
