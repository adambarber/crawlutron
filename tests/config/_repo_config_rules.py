"""Codecov and pyproject rules over the typed config models."""

from __future__ import annotations

from collections.abc import Iterator

from tests._walk_primitives import member_import_names
from tests.config._bundle import Bundle, _finding, _job
from tests.config._policy import Finding, RuleId


def codecov_components(bundle: Bundle) -> Iterator[Finding]:
    """Six components: three surfaces from ci's coverage needs, three packages."""
    codecov = bundle.codecov
    coverage = _job(bundle, "ci", "coverage")
    needs = set(coverage.needs) if coverage else set()
    surfaces = needs - {"changes"}
    packages = frozenset({"admission", "crawl", "sdk"})
    expected = surfaces | packages
    if not codecov.default_carryforward:
        yield _finding(
            RuleId.CODECOV_COMPONENTS, codecov.path.name, 1, "default_rules.carryforward must be true"
        )
    missing = expected - codecov.component_names
    if missing:
        yield _finding(
            RuleId.CODECOV_COMPONENTS,
            codecov.path.name,
            1,
            f"individual_components missing {sorted(missing)}",
        )
    for name in sorted(surfaces & codecov.component_names):
        component = codecov.components[name]
        if component.flag_regexes != (name,) or not component.carryforward:
            yield _finding(
                RuleId.CODECOV_COMPONENTS,
                codecov.path.name,
                1,
                f"component {name!r} needs flag_regexes=[{name!r}] and carryforward",
            )
    for name in sorted(packages & codecov.component_names):
        component = codecov.components[name]
        if not component.paths or not component.carryforward:
            yield _finding(
                RuleId.CODECOV_COMPONENTS,
                codecov.path.name,
                1,
                f"component {name!r} needs paths and carryforward",
            )


def codecov_informational(bundle: Bundle) -> Iterator[Finding]:
    """Status is informational; a blocking threshold or flag_management is a defect."""
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
    if codecov.flag_management is not None:
        yield _finding(
            RuleId.CODECOV_INFORMATIONAL,
            codecov.path.name,
            1,
            "flag_management is retired; use component_management",
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
