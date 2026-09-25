"""Guards for ``app.api.webhooks`` after the dead-delivery-island deletion (H3).

The crawl-plus-deliver story lives solely in ``app.api.async_crawl_runner``;
``app.api.webhooks`` is the delivery funnel (``post_webhook`` ->
``deliver_pinned_webhook``) plus the URL validator. A second module-level
dependency-wiring apparatus in the webhook module would recreate the island
that shipped a duplicate crawl path with zero production callers, so its
absence is pinned here.
"""

from __future__ import annotations

import ast
import pathlib

from chokepoint_walks import nodes_of

from app.api import webhooks as wm

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
    """The deleted island must not come back.

    Every name the island needed is gone from the module: no globals to wire,
    no setters, no second crawl-composing entry point. The live surface is
    exactly the funnel plus the validator.
    """
    for name in ISLAND_NAMES:
        assert not hasattr(wm, name), f"dead-island name reappeared: {name}"
    assert set(wm.__all__) == {
        "deliver_pinned_webhook",
        "post_webhook",
        "validate_webhook_url",
        "validate_webhook_url_async",
    }


def test_deliver_pinned_webhook_is_called_only_from_post_webhook():
    """One POST funnel: only ``post_webhook`` may invoke the pinned delivery.

    This is the shape the deleted island violated -- a second crawler reached
    the same delivery helper by composing its own crawl. If a caller other
    than ``post_webhook`` appears inside the module (or any other module
    calls the helper), the funnel is no longer the only path and the
    size-cap/validation story can drift from it.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    callers: set[str] = set()
    for path in sorted((repo_root / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in nodes_of(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in nodes_of(node):
                if (
                    isinstance(inner, ast.Call)
                    and getattr(inner.func, "id", "")
                    in {"deliver_pinned_webhook"}
                ):
                    callers.add(f"{path.name}::{node.name}")
    assert callers == {"webhooks.py::post_webhook"}, callers


def test_build_job_status_handles_single_shot_jobs():
    """SINGLE_SHOT jobs have empty frontier/visited (dataclass defaults).

    The reviewer was concerned that ``len(job.frontier)`` would raise
    AttributeError for SINGLE_SHOT jobs. Verified: ``frontier`` and
    ``visited`` are dataclass fields with default_factory, so they are
    always present even for jobs that never use them.
    """
    from app.api.main_helpers import _build_job_status
    from app.api_models import JobStatusResponse
    from app.crawl.job_store import CrawlJobState, JobKind

    job = CrawlJobState(
        job_id="single_test",
        kind=JobKind.SINGLE_SHOT,
        root_url="http://example.com/",
        policy={},
    )
    status = _build_job_status(job)
    assert isinstance(status, JobStatusResponse)
    assert status.frontier_size == 0
    assert status.visited_size == 0
    assert status.results_count == 0
    assert status.job_id == "single_test"
