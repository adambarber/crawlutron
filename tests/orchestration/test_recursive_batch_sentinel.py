"""#515 red-first: the recursive batch cap must keep the unbounded sentinel.

The pre-fix ``max(1, ...)`` clamped an unbounded (0) crawl to one page/pass;
the fix reads the cap through ``remaining_page_capacity`` (no term when 0).
"""

from app.orchestration.recursive import admit_recursive_batch
from app.runtime.metrics import get_metrics
from tests._admission_runs import minimum_admission_run as _minimum_admission_run
from tests._recursive_harnesses import make_crawl_task


async def test_an_unbounded_recursive_crawl_is_not_clamped_to_one_page_per_pass():
    """``max_pages <= 0`` is the unbounded sentinel -- a recursive crawl must
    admit a real batch per pass, never the clamped one-page-per-pass budget."""
    tasks = [make_crawl_task(i, f"h{i}") for i in range(4)]
    run, state, admission = _minimum_admission_run(tasks, batch_pages=0, http_quota=10, browser_quota=10)

    selected = await admit_recursive_batch(run, state, get_metrics())

    assert len(selected) == 4, "the unbounded crawl was sized as if it were capped"
    assert admission.last_want == (4, 0)


async def test_the_unbounded_sentinel_admits_beyond_the_first_pass():
    """The sentinel survives committed pages -- unbounded means unbounded on
    every pass, not just the first one where the subtraction is still >= 0."""
    tasks = [make_crawl_task(i, f"h{i}") for i in range(4)]
    run, state, _admission = _minimum_admission_run(tasks, batch_pages=0, http_quota=10, browser_quota=10)
    state.page_count = 3

    selected = await admit_recursive_batch(run, state, get_metrics())

    assert len(selected) == 4
