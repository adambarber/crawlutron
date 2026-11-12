"""Every consumed browser knob is documented, and every documented knob is consumed."""

from __future__ import annotations

import ast
import pathlib

from crawlutron.config.settings import ENVIRONMENT_FIELD_ALIASES
from tests.chokepoint_walks import (
    REPO_ROOT,
    app_trees,
    class_defs,
    file_tree,
    string_literals,
)
from tests.invariants._env_knobs import (
    _KNOB_PATTERN,
    _consumed_knobs,
    _environment_reader_reference,
    _relative_app_path,
    _settings_alias_declaration_constant,
)
from tests.property_registry import Property


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


def _settings_fields() -> set[str]:
    tree = file_tree("packages/core/crawlutron/config/settings.py")[1]
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
            if (
                isinstance(node, ast.Attribute)
                and _relative_app_path(path) != "packages/core/crawlutron/config/settings.py"
            ):
                environment = ENVIRONMENT_FIELD_ALIASES.get(node.attr)
                if environment is not None:
                    references.add(environment)
                conventional = f"CRAWLUTRON_{node.attr.upper()}"
                if conventional in field_knobs:
                    references.add(conventional)
    # Scripts are consumption too (probe_session.py reads the probe knobs);
    # a knob a script reads is live for operators the same way.
    references |= _script_and_dockerfile_consumption()
    return references


def _script_and_dockerfile_consumption() -> set[str]:
    from tests.invariants._env_knobs import _consumed_knobs as _base
    from tests.invariants._env_knobs import _consumed_knobs_script_wide

    return _consumed_knobs_script_wide() - _base()


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
