"""The handler-factory collapse invariant (#ARCH-1, data-driven dispatch off ToolSpec).

Four factory families produced near-identical handle_* bindings. The collapse
lands DoorBinding data on ToolSpec and ONE dispatch function, so the handler
factories and their module-level handle_* aliases are gone.
"""

from __future__ import annotations

from tests.chokepoint_walks import REPO_ROOT, parsed
from tests.invariants.test_public_surface import _owned_public_symbols

_HANDLERS = REPO_ROOT / "crawlutron/mcp/handlers.py"

#: The bindings the factories produced, keyed by the tool they served.
_FACTORY_BINDINGS = frozenset(
    {
        "handle_crawl",
        "handle_crawl_and_extract",
        "handle_read_crawl_result",
        "handle_read_crawl_trace",
        "handle_list_jobs",
        "handle_get_job",
        "handle_control_job",
        "handle_delete_job",
        "handle_register_watch",
        "handle_list_watches",
        "handle_get_watch",
        "handle_delete_watch",
    }
)

_FACTORY_FUNCTIONS = frozenset(
    {
        "_crawl_tool_handler",
        "_read_tool_handler",
        "_job_tool_handler",
        "_watch_tool_handler",
    }
)


def test_no_factory_bindings_or_factories_survive():
    """Every handle_* the factories produced is a deleted binding, and no
    factory function remains in handlers.py."""
    names = _owned_public_symbols(parsed(_HANDLERS))
    leaked = sorted((names & _FACTORY_BINDINGS) | (names & _FACTORY_FUNCTIONS))
    assert not leaked, f"factory machinery survived the collapse: {leaked}"
