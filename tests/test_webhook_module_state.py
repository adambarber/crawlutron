"""Guards for ``crawlutron.api.webhooks`` after the dead-delivery-island deletion: the
crawl-plus-deliver story lives in ``async_crawl_runner``, and a second wiring
apparatus here would recreate the island -- so its absence is pinned.
"""

from __future__ import annotations

import ast
import pathlib

from crawlutron.api import webhooks as wm
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
        "ensure_https_webhook_url",
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
    for path in sorted((repo_root / "crawlutron").rglob("*.py")):
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
