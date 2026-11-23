"""Codecov and pyproject rules over the typed config models."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath

from tests._walk_primitives import import_roots, member_import_names
from tests.config._bundle import Bundle, _finding
from tests.config._model import Codecov
from tests.config._policy import Finding, RuleId

_ALLOWED_CARRYFORWARD = frozenset({"flag_management", "flags"})


def _component_shape(codecov: Codecov) -> Iterator[Finding]:
    if not codecov.components:
        yield _finding(RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, "individual_components is empty")
    for name, component in sorted(codecov.components.items()):
        if not name:
            yield _finding(RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, "component_id is required")
        if component.carryforward:
            yield _finding(
                RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, f"component {name!r} carries carryforward"
            )
        if component.flag_regexes:
            yield _finding(
                RuleId.CODECOV_COMPONENTS,
                codecov.path.name,
                1,
                f"component {name!r} must be path-based, not flag_regexes",
            )
        if not component.paths:
            yield _finding(RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, f"component {name!r} needs paths")


def _derived_members(codecov: Codecov) -> Iterator[Finding]:
    expected = {PurePosixPath(root).name: f"{root}/**" for root in import_roots()}
    missing = set(expected) - codecov.component_names
    if missing:
        yield _finding(
            RuleId.CODECOV_COMPONENTS,
            codecov.path.name,
            1,
            f"individual_components missing derived members {sorted(missing)}",
        )
    for name, glob in expected.items():
        component = codecov.components.get(name)
        if component is not None and component.paths != (glob,):
            yield _finding(
                RuleId.CODECOV_COMPONENTS,
                codecov.path.name,
                1,
                f"component {name!r} paths must be ({glob!r},)",
            )


def _carryforward(codecov: Codecov) -> Iterator[Finding]:
    for site in codecov.carryforward_sites:
        if not site or site[0] not in _ALLOWED_CARRYFORWARD:
            yield _finding(
                RuleId.CODECOV_COMPONENTS,
                codecov.path.name,
                1,
                f"carryforward at {'.'.join(site)} outside flag_management/flags",
            )
    if not codecov.flag_carryforward:
        yield _finding(
            RuleId.CODECOV_COMPONENTS,
            codecov.path.name,
            1,
            "flag_management.default_rules.carryforward must be true",
        )
    if codecov.nightly_joined is not False:
        yield _finding(RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, "flags.nightly.joined must be false")


def codecov_components(bundle: Bundle) -> Iterator[Finding]:
    """Path-based components cover every uv member and carry no carryforward."""
    codecov = bundle.codecov
    if not codecov.individual_components_is_list:
        yield _finding(
            RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, "individual_components must be a list"
        )
        return
    yield from _component_shape(codecov)
    yield from _derived_members(codecov)
    yield from _carryforward(codecov)


def codecov_informational(bundle: Bundle) -> Iterator[Finding]:
    """Status is informational; a blocking threshold is a defect."""
    codecov = bundle.codecov
    for label, status in (("project", codecov.project), ("patch", codecov.patch)):
        if not status.informational:
            yield _finding(
                RuleId.CODECOV_INFORMATIONAL, codecov.path.name, 1, f"{label} status must be informational"
            )
        if label == "project" and status.threshold not in (None, "0.5%"):
            yield _finding(
                RuleId.CODECOV_INFORMATIONAL,
                codecov.path.name,
                1,
                f"unexpected project threshold {status.threshold!r}",
            )


def coverage_source_pkgs(bundle: Bundle) -> Iterator[Finding]:
    """source_pkgs must name exactly the uv workspace's import names."""
    run = bundle.pyproject.coverage_run
    expected = frozenset(member_import_names())
    if run.source_pkgs != expected:
        yield _finding(
            RuleId.COVERAGE_SOURCE_PKGS,
            bundle.pyproject.path.name,
            1,
            f"source_pkgs {sorted(run.source_pkgs)} != members {sorted(expected)}",
        )


def coverage_measure_once(bundle: Bundle) -> Iterator[Finding]:
    """Measure-once coverage: sysmon core, relative parallel data, no deeper knobs."""
    run = bundle.pyproject.coverage_run
    expected = {
        "core": "sysmon",
        "branch": False,
        "relative_files": True,
        "parallel": True,
        "dynamic_context": False,
    }
    for name, value in expected.items():
        if getattr(run, name) != value:
            yield _finding(
                RuleId.COVERAGE_MEASURE_ONCE,
                bundle.pyproject.path.name,
                1,
                f"[tool.coverage.run] {name} must be {value!r}",
            )


def ruff_known_first_party(bundle: Bundle) -> Iterator[Finding]:
    """isort names every member import plus the two local trees; D209 stays gone."""
    ruff = bundle.pyproject.ruff
    expected = frozenset(member_import_names()) | {"tests", "scripts"}
    missing = expected - ruff.known_first_party
    if missing:
        yield _finding(
            RuleId.RUFF_KNOWN_FIRST_PARTY,
            bundle.pyproject.path.name,
            1,
            f"known-first-party missing {sorted(missing)}",
        )
    if "D209" in ruff.ignored:
        yield _finding(
            RuleId.RUFF_KNOWN_FIRST_PARTY, bundle.pyproject.path.name, 1, "the dead D209 ignore came back"
        )
