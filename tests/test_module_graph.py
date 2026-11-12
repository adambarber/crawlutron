"""The module graph's edge set is runtime dependencies, not type-checking lies."""

from __future__ import annotations

import ast

from scripts.module_graph import _imports

# Real modules of this tree, so _resolve's on-disk check accepts them: they stand
# in for any two modules whose only mutual reference is a type-checking one.
A, B = "crawlutron.api.crawl_endpoint", "crawlutron.api.caller_auth"

_ALPHA = (
    "from __future__ import annotations\n"
    "from typing import TYPE_CHECKING\n"
    f"if TYPE_CHECKING:\n    from {B} import CallerAuth\n"
    "class Alpha: pass\n"
)
_BETA = (
    "from __future__ import annotations\n"
    "from typing import TYPE_CHECKING\n"
    f"if TYPE_CHECKING:\n    from {A} import CrawlEndpoint\n"
    "class Beta: pass\n"
)


def _imports_of(source: str) -> set[str]:
    return _imports(ast.parse(source))


def test_a_type_checking_only_cycle_is_not_reported() -> None:
    """At HEAD the migrations manifest <--> sqlite_store cycle was a phantom:
    its edges lived entirely under TYPE_CHECKING. The graph must not report one."""
    assert _imports_of(_ALPHA) == set()
    assert _imports_of(_BETA) == set()


def test_a_runtime_edge_survives_the_type_checking_skip() -> None:
    """The skip removes type-checking edges, not the module's real ones: a file
    that imports at runtime AND under TYPE_CHECKING keeps only the runtime edge.
    """
    source = (
        "from typing import TYPE_CHECKING\n"
        f"from {A} import CrawlEndpoint\n"
        f"if TYPE_CHECKING:\n    from {B} import CallerAuth\n"
    )
    assert _imports_of(source) == {A}


def test_a_runtime_edge_is_reported() -> None:
    """A plain runtime import is still an edge."""
    assert _imports_of(f"from {A} import CrawlEndpoint\n") == {A}


def test_a_package_form_import_records_the_edge_into_the_package() -> None:
    """`from crawlutron.api import exceptions` reaches the crawlutron.api package
    (a namespace dir with no __init__.py); the MCP seal's forbidden layer must see it."""
    assert _imports_of("from crawlutron.api import exceptions\n") == {"crawlutron.api.exceptions"}


def test_a_from_package_import_alias_resolves_the_subpackage() -> None:
    """`from crawlutron import api` is the same blind-spot class: the alias names
    the crawlutron.api package dir, so the seal must see that edge too."""
    assert _imports_of("from crawlutron import api\n") == {"crawlutron.api"}


def test_a_module_form_import_still_resolves_the_module_file() -> None:
    """Regression: module-form `from crawlutron.api.crawl_endpoint import ...` keeps
    resolving to the .py module file, not to a package dir."""
    assert _imports_of("from crawlutron.api.crawl_endpoint import CrawlEndpoint\n") == {
        "crawlutron.api.crawl_endpoint"
    }
