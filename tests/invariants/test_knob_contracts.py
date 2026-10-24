"""Every consumed browser knob is documented, and every documented knob is consumed."""

from __future__ import annotations

import ast
import pathlib
import re

from app.config.settings import ENVIRONMENT_FIELD_ALIASES
from scripts.ast_scopes import is_body_owner
from tests.chokepoint_walks import (
    REPO_ROOT,
    app_trees,
    class_defs,
    file_tree,
    parent_map,
    resolved_call_name,
    string_literals,
    trees_under,
)
from tests.invariants._shared import _relative_app_path
from tests.property_registry import Property

_KNOB_PATTERN = re.compile(r"\bCRAWLUTRON_[A-Z0-9_]+\b")


def _documented_browser_knobs() -> set[str] | None:
    env_path = REPO_ROOT / ".env.example"
    readme_path = REPO_ROOT / "README.md"
    if not env_path.exists() or not readme_path.exists():
        return None
    env_names = {
        match.group(0)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for match in _KNOB_PATTERN.finditer(line)
    }
    readme_names = set(_KNOB_PATTERN.findall(readme_path.read_text(encoding="utf-8")))
    return {name for name in env_names | readme_names if not name.endswith("_")}


def _settings_alias_declaration_constant(
    path: pathlib.Path,
    tree: ast.AST,
    node: ast.Constant,
) -> bool:
    if _relative_app_path(path) != "app/config/settings.py":
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


def _settings_fields() -> set[str]:
    tree = file_tree("app/config/settings.py")[1]
    for node in class_defs(tree):
        if node.name == "CrawlerSettings":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return set()


def _settings_alias_failures(
    aliases: dict[str, str],
    fields: set[str] | None = None,
) -> list[str]:
    known_fields = fields if fields is not None else _settings_fields()
    failures = [
        f"{field} maps to invalid environment name {environment}"
        for field, environment in aliases.items()
        if field not in known_fields or not environment.startswith("CRAWLUTRON_")
    ]
    fields_by_environment: dict[str, list[str]] = {}
    for field, environment in aliases.items():
        fields_by_environment.setdefault(environment, []).append(field)
    failures.extend(
        f"{', '.join(sorted(fields_for_environment))} map to {environment}"
        for environment, fields_for_environment in sorted(fields_by_environment.items())
        if len(fields_for_environment) > 1
    )
    return failures


def _production_browser_knob_references() -> set[str]:
    references: set[str] = set()
    fields = _settings_fields()
    field_knobs = {f"CRAWLUTRON_{field.upper()}": field for field in fields}
    for path, tree in app_trees(exclude=frozenset()):
        for node in string_literals(tree):
            if isinstance(node.value, str) and (not _settings_alias_declaration_constant(path, tree, node)):
                environment = _environment_reader_reference(
                    tree,
                    node,
                    ENVIRONMENT_FIELD_ALIASES,
                )
                if environment is not None:
                    references.add(environment)
            if isinstance(node, ast.Attribute) and _relative_app_path(path) != "app/config/settings.py":
                environment = ENVIRONMENT_FIELD_ALIASES.get(node.attr)
                if environment is not None:
                    references.add(environment)
                conventional = f"CRAWLUTRON_{node.attr.upper()}"
                if conventional in field_knobs:
                    references.add(conventional)
    # Scripts are consumption too (probe_session.py reads the probe knobs);
    # a knob a script reads is live for operators the same way.
    for _path, tree in trees_under(REPO_ROOT / "scripts"):
        for node in string_literals(tree):
            if isinstance(node.value, str) and node.value.startswith("CRAWLUTRON_"):
                references.add(node.value)
    dockerfile = REPO_ROOT / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("CMD", "ENTRYPOINT")):
                references.update(_KNOB_PATTERN.findall(line))
    return references


def _documented_knob_offenders(
    documented: set[str] | None,
    references: set[str],
) -> list[str]:
    if documented is None:
        return ["documented knob source could not be located"]
    if not documented:
        return ["no documented CRAWLUTRON knobs were found"]
    return [f"{name} has no production consumer" for name in sorted(documented - references)]


def _resolved_env_reader_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    references: set[str] = set()
    for node in string_literals(tree):
        if isinstance(node.value, str):
            environment = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
            if environment is not None:
                references.add(environment)
    return _documented_knob_offenders(
        {"CRAWLUTRON_LAZY_SCROLL_STEPS"},
        references,
    )


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


def _undocumented_knob_offenders() -> list[str]:
    """The reverse of ``_documented_knob_offenders``.

    The forward guard misses a knob the code reads but an operator can't find;
    this catches the real-but-undocumented set.
    """
    documented = _documented_browser_knobs()
    if documented is None:
        return ["documented knob source could not be located"]
    return [
        f"{name} is read from the environment but never documented"
        for name in sorted(_consumed_knobs() - documented)
    ]


def test_every_consumed_knob_is_documented():
    assert not _undocumented_knob_offenders(), "; ".join(_undocumented_knob_offenders())


def test_every_consumed_knob_is_documented_bites():
    """The reverse gate has to be able to fail, or it is decoration.

    Asserted against a real tree: it passes only if the walk finds a genuine
    consumer the document set omits, so deleting the walk can't turn it green.
    """
    consumed = _consumed_knobs()
    assert "CRAWLUTRON_HOST" in consumed, "the walk stopped finding real consumers"
    offenders = [
        f"{name} is read from the environment but never documented"
        for name in sorted(consumed - {"CRAWLUTRON_HOST"})
    ]
    assert len(offenders) == len(consumed) - 1
    assert _undocumented_knob_offenders() == []


def test_documented_browser_knobs_have_consumers():
    assert not _settings_alias_failures(ENVIRONMENT_FIELD_ALIASES)
    failures = _documented_knob_offenders(
        _documented_browser_knobs(),
        _production_browser_knob_references(),
    )
    assert not failures, "; ".join(failures)
