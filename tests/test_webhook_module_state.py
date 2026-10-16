"""Guards for ``app.api.webhooks`` after the dead-delivery-island deletion: the
crawl-plus-deliver story lives in ``async_crawl_runner``, and a second wiring
apparatus here would recreate the island -- so its absence is pinned.
"""

from __future__ import annotations

import ast
import pathlib

from app.api import webhooks as wm
from app.models.crawl_policy import CrawlPolicy
from tests.chokepoint_walks import nodes_of

ISLAND_NAMES = (
    "run_and_deliver_webhook",
    "deliver_webhook",
    "set_webhook_dependencies",
    "clear_webhook_dependencies",
    "_webhook_semaphore",
    "_shutdown_event",
    "_crawl_request_fn",
    "_pool_getter",
    "_get_metrics_fn",
)


def test_webhook_module_has_no_dependency_wiring_state():
    """The deleted island must not come back: every name it needed -- no globals, no
    setters, no second crawl-composing entry point -- is gone, leaving exactly the
    funnel plus the validator.
    """
    for name in ISLAND_NAMES:
        assert not hasattr(wm, name), f"dead-island name reappeared: {name}"
    assert set(wm.__all__) == {
        "deliver_pinned_webhook",
        "max_webhook_size",
        "post_webhook",
        "validate_webhook_url",
        "validate_webhook_url_async",
    }


def test_deliver_pinned_webhook_is_called_only_from_post_webhook():
    """One POST funnel: only ``post_webhook`` may invoke the pinned delivery -- the
    shape the deleted island violated by composing its own crawl; a second caller
    means the size-cap/validation story can drift from the funnel.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    callers: set[str] = set()
    for path in sorted((repo_root / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in nodes_of(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in nodes_of(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") in {
                    "deliver_pinned_webhook"
                }:
                    callers.add(f"{path.name}::{node.name}")
    assert callers == {"webhooks.py::post_webhook"}, callers


def test_build_job_status_handles_single_shot_jobs(tmp_path):
    """SINGLE_SHOT jobs report zero counts through the real store: the counts are the
    store's projection now, not ``len()`` over dataclass fields, so the AttributeError
    concern is gone but the invariant is pinned against production code.
    """
    from app.api.main_helpers import build_job_status
    from app.models.job_status_response import JobStatusResponse
    from tests._job_harnesses import make_job_store

    store = make_job_store(tmp_path)
    job = store.create_single_shot("single_test", "http://example.com/", CrawlPolicy())

    row = store.status_row(job.job_id)
    assert row is not None
    status = build_job_status(row)

    assert isinstance(status, JobStatusResponse)
    assert status.frontier_size == 0
    assert status.visited_size == 0
    assert status.results_count == 0
    assert status.job_id == "single_test"
