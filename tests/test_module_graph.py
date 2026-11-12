"""The module graph's edge set is runtime dependencies, not type-checking lies."""

from __future__ import annotations

import ast

import pytest

from scripts import module_graph
from scripts.module_graph import _imports, build_graph
from tests._walk_primitives import import_roots

# Real modules of this tree, so _resolve's on-disk check accepts them: they stand
# in for any two modules whose only mutual reference is a type-checking one.
A, B = "crawlutron.models.host_policy", "crawlutron.vocabulary.identity"

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


def test_a_package_dir_with_only_subpackages_still_resolves(tmp_path, monkeypatch) -> None:
    """A package dir holding only subpackage .py files (no direct child) must
    still resolve, at any depth — the one-level glob blind-spot is closed."""
    nested = tmp_path / "crawlutron" / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "leaf.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(module_graph, "_MEMBERS", (tmp_path,))
    monkeypatch.setattr(module_graph, "_IMPORT_NAMES", {"crawlutron"})
    assert module_graph._resolve("crawlutron.pkg") == "crawlutron.pkg"


def test_a_vacuous_app_root_is_refused_not_greened(tmp_path, monkeypatch) -> None:
    """The seal kill-switch: an empty import root builds no graph, and build_graph raises,
    so the MCP/service layering seals cannot green over a scanned-nothing walk."""
    empty = tmp_path / "crawlutron"
    empty.mkdir()
    monkeypatch.setattr(module_graph, "IMPORT_ROOTS", ((empty, "crawlutron"),))
    with pytest.raises(AssertionError, match="walked zero modules"):
        module_graph.build_graph()


def test_a_package_form_import_records_the_edge_into_the_package() -> None:
    """`from crawlutron.service import errors` reaches the crawlutron.service package
    (a namespace dir with no __init__.py); the seal's forbidden layer must see it."""
    assert _imports_of("from crawlutron.service import errors\n") == {"crawlutron.service.errors"}


def test_a_from_package_import_alias_resolves_the_subpackage() -> None:
    """`from crawlutron import service` is the same blind-spot class: the alias names
    the crawlutron.service package dir, so the seal must see that edge too."""
    assert _imports_of("from crawlutron import service\n") == {"crawlutron.service"}


def test_a_module_form_import_still_resolves_the_module_file() -> None:
    """Regression: module-form `from crawlutron.service.errors import ...` keeps
    resolving to the .py module file, not to a package dir."""
    assert _imports_of("from crawlutron.service.errors import DomainError\n") == {"crawlutron.service.errors"}


def test_the_graph_walks_every_import_root_not_just_crawlutron() -> None:
    """Wave A (#917): the graph must walk every derived import root, so a package that
    leaves core (mcp -> packages/mcp/src/crawlutron_mcp) is still walked and its seal never
    greens over an empty set. Red-first: the single-package graph named only crawlutron."""
    modules, _importers = build_graph()
    prefixes = {name.split(".", 1)[0] for name in modules}
    derived = {root.rsplit("/", 1)[-1] for root in import_roots()}
    missing = sorted(derived - prefixes)
    assert not missing, (
        "the module graph walks only a subset of the derived import roots; an "
        "unwalked root's sealed layer would green over empty (missing: " + repr(missing) + ")"
    )


def test_the_walk_pins_each_canonical_package_root_vs_a_vanishing_member() -> None:
    """Pin the canonical package roots: the derived-vs-walked check greens when a member
    vanishes from BOTH sides together, so a disappeared mcp must red the walk explicitly."""
    modules, _importers = build_graph()
    prefixes = {name.split(".", 1)[0] for name in modules}
    canonical = {"admission", "crawlutron", "crawlutron_mcp", "crawlutron_rest", "crawlutron_sdk"}
    not_walked = sorted(canonical - prefixes)
    assert not not_walked, (
        "the module graph no longer walks these canonical package roots; a vanished "
        "root's sealed layer would green over empty (not walked: " + repr(not_walked) + ")"
    )
