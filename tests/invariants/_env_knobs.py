"""The environment-knob walk the documentation gates share.

Declared support surface (#476): the knob-contract, doc-contract and env-documentation modules import
these from here, not from each other's internals. Bodies moved verbatim from test_knob_contracts.py.
"""

from __future__ import annotations

import ast
import re

from crawlutron.config.settings import ENVIRONMENT_FIELD_ALIASES
from scripts.ast_scopes import is_body_owner
from tests.chokepoint_walks import (
    REPO_ROOT,
    app_trees,
    parent_map,
    resolved_call_name,
    string_literals,
    trees_under,
)
from tests.invariants._shared import _relative_app_path

__all__ = ["_KNOB_PATTERN", "_consumed_knobs"]

_KNOB_PATTERN = re.compile(r"\bCRAWLUTRON_[A-Z0-9_]+\b")

_ENV_READER_NAMES = frozenset(
    {
        "get",
        "getenv",
        "get_env_float",
        "get_env_int",
        "get_mapping_float",
        "get_mapping_int",
        "parse_bool_env",
        "parse_bool_value",
        "EnvDecisionMemo",
    }
)


def _first_statement_of_block(scope: ast.AST) -> ast.stmt | None:
    """The first statement of a block-owning node, or None when it owns none."""
    if not is_body_owner(scope):
        return None
    body = scope.body
    return body[0] if body else None


def _environment_reader_reference(
    tree: ast.AST,
    node: ast.Constant,
    field_environments: dict[str, str],
) -> str | None:
    parents = parent_map(tree)
    expression = parents.get(id(node))
    scope = parents.get(id(expression)) if expression is not None else None
    # The block-owning statement kinds, named rather than ``ast.stmt``: "is this the first statement of a
    # block" asks for a body, and only some statements have one.
    if (
        isinstance(node, ast.Constant)
        and isinstance(expression, ast.Expr)
        and expression.value is node
        and scope is not None
        and _first_statement_of_block(scope) is expression
    ):
        return None
    current: ast.AST = node
    alias_value: str | None = None
    is_alias_lookup = False
    reaches_reader = False
    while id(current) in parents:
        current = parents[id(current)]
        if (
            isinstance(current, ast.Subscript)
            and isinstance(current.value, ast.Name)
            and current.value.id == "ENVIRONMENT_FIELD_ALIASES"
        ):
            is_alias_lookup = True
            if isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
                alias_value = field_environments.get(current.slice.value)
        if (
            isinstance(current, ast.Call)
            and resolved_call_name(tree, current).rsplit(".", 1)[-1] in _ENV_READER_NAMES
        ):
            reaches_reader = True
    if not reaches_reader:
        return None
    if is_alias_lookup:
        return alias_value
    # ast.Constant.value is the full literal union, so a prefix test is a claim about the value being a str,
    # not about the node being a Constant.
    if isinstance(node.value, str) and node.value.startswith("CRAWLUTRON_"):
        return node.value
    return None


def _settings_alias_declaration_constant(
    path,
    tree,
    node,
) -> bool:
    if _relative_app_path(path) != "packages/core/crawlutron/config/settings.py":
        return False
    parents = parent_map(tree)
    current: ast.AST = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ENVIRONMENT_FIELD_ALIASES"
            for target in current.targets
        ):
            return True
    return False


def _consumed_knobs() -> set[str]:
    """Every ``CRAWLUTRON_*`` name app/ actually reads from the environment.

    The reader detector is the same one the forward direction uses, so the two
    agree by construction; it already declines docstring-only mentions.
    """
    names: set[str] = set()
    for _path, tree in app_trees():
        for node in string_literals(tree):
            if isinstance(node.value, str):
                reference = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
                if reference is not None:
                    names.add(reference)
    return names


def _consumed_knobs_script_wide() -> set[str]:
    """The same walk including scripts/ and Dockerfile consumption."""
    names = _consumed_knobs()
    for _path, tree in trees_under(REPO_ROOT / "scripts"):
        for node in string_literals(tree):
            if isinstance(node.value, str) and node.value.startswith("CRAWLUTRON_"):
                names.add(node.value)
    dockerfile = REPO_ROOT / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("CMD", "ENTRYPOINT")):
                names.update(_KNOB_PATTERN.findall(line))
    return names
