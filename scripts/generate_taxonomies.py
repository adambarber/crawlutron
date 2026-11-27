"""Render taxonomy projections from their one declaration; never hand-edit output."""

from __future__ import annotations

import argparse
import ast
import pathlib

from tests._walk_primitives import discovered_files
from tests.chokepoint_paths import REPO_ROOT

_TAXONOMY = "packages/core/src/crawlutron/vocabulary/taxonomy.py"
_STRATEGIES = "packages/core/src/crawlutron/vocabulary/strategies.py"

_STATUS_PATH = "packages/core/src/crawlutron/models/crawl_status.py"
_STRATEGY_PATH = "packages/core/src/crawlutron/models/crawl_strategy.py"
_STRATEGY_NAME_PATH = "packages/core/src/crawlutron/models/crawl_strategy_name.py"
_SDK_PATH = "packages/sdk/src/crawlutron_sdk/generated_taxonomies.py"

_STATUS_DOC = (
    '"""How one fetch ended.\n\n'
    "    Orthogonal to the taxonomy's Classification, which records whether a host\n"
    "    was reachable rather than what happened to the bytes.\n"
    '    """'
)
_STRATEGY_DOC = (
    '"""Fetch execution strategies, tried in fallback order.\n\n'
    "    These say HOW to retrieve bytes for a URL; parsing them into text/markdown is\n"
    "    a separate, orthogonal concern handled by ``CrawlExtractionMethod``.\n"
    '    """'
)


def _source(root: pathlib.Path, relative: str) -> str:
    """One declaration's text, read through the shared filesystem door."""
    path = root / relative
    found = discovered_files(path.parent, path.name, include_content=True)
    if not isinstance(found, dict):
        raise TypeError(f"{relative}: declaration source is missing")
    return found[path]


def _tree(root: pathlib.Path, relative: str) -> ast.Module:
    """Parse a declaration without importing it."""
    return ast.parse(_source(root, relative))


def _assignment(node: ast.stmt) -> tuple[str, ast.expr | None] | None:
    """The name a module-level statement assigns, and the value assigned."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id, node.value
    return None


def classification_values(root: pathlib.Path) -> tuple[str, ...]:
    """The classification members in declaration order."""
    tree = _tree(root, _TAXONOMY)
    enum = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CrawlClassification"
    )
    values: list[str] = []
    for node in enum.body:
        assigned = _assignment(node)
        if assigned and isinstance(assigned[1], ast.Constant) and isinstance(assigned[1].value, str):
            values.append(assigned[1].value)
    return tuple(values)


def result_status_values(root: pathlib.Path) -> tuple[str, ...]:
    """The result-status Literal in declaration order."""
    tree = _tree(root, _TAXONOMY)
    literal: ast.expr | None = None
    for node in tree.body:
        assigned = _assignment(node)
        if assigned is not None and assigned[0] == "ResultStatus":
            literal = assigned[1]
            break
    if not (isinstance(literal, ast.Subscript) and isinstance(literal.value, ast.Name)):
        raise TypeError("ResultStatus is no longer a Literal declaration")
    args = literal.slice.elts if isinstance(literal.slice, ast.Tuple) else (literal.slice,)
    return tuple(arg.value for arg in args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))


def strategy_values(root: pathlib.Path) -> tuple[str, ...]:
    """The strategy constants in the declared DEFAULT_STRATEGIES order."""
    tree = _tree(root, _STRATEGIES)
    constants: dict[str, str] = {}
    order: list[str] = []
    for node in tree.body:
        assigned = _assignment(node)
        if assigned is None:
            continue
        name, value = assigned
        if name == "DEFAULT_STRATEGIES" and isinstance(value, ast.List):
            order = [elt.id for elt in value.elts if isinstance(elt, ast.Name)]
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[name] = value.value
    return tuple(constants[name] for name in order)


def _enum(name: str, values: tuple[str, ...], doc: str, source: str) -> str:
    """One StrEnum projection."""
    members = "\n".join(f'    {value} = "{value}"' for value in values)
    return (
        f'"""Generated from {source}; do not edit."""\n\n'
        "from __future__ import annotations\n\n"
        "from enum import StrEnum\n\n\n"
        f"class {name}(StrEnum):\n"
        f"    {doc}\n\n"
        f"{members}\n"
    )


def _literal(name: str, values: tuple[str, ...], source: str) -> str:
    """One Literal projection."""
    members = "\n".join(f'    "{value}",' for value in values)
    return (
        f'"""Generated from {source}; do not edit."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Literal\n\n"
        f"{name} = Literal[\n"
        f"{members}\n"
        "]\n"
    )


def render_all(root: pathlib.Path) -> list[tuple[str, str]]:
    """Every projection as its relative path and source text."""
    classifications = classification_values(root)
    statuses = result_status_values(root)
    strategies = strategy_values(root)
    return [
        (_STATUS_PATH, _enum("CrawlStatus", statuses, _STATUS_DOC, _TAXONOMY)),
        (_STRATEGY_PATH, _enum("CrawlStrategy", strategies, _STRATEGY_DOC, _STRATEGIES)),
        (_STRATEGY_NAME_PATH, _literal("CrawlStrategyName", strategies, _STRATEGIES)),
        (_SDK_PATH, _literal("WatchOutcome", classifications, _TAXONOMY)),
    ]


def write_all(root: pathlib.Path) -> None:
    """Regenerate every projection in place."""
    for relative, text in render_all(root):
        (root / relative).write_text(text)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when a projection is stale")
    args = parser.parse_args()
    if args.check:
        stale = [rel for rel, text in render_all(REPO_ROOT) if _source(REPO_ROOT, rel) != text]
        if stale:
            parser.exit(1, "stale projections: " + ", ".join(stale) + "\n")
        return 0
    write_all(REPO_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
