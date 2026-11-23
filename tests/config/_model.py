"""Strict typed projections of codecov.yml and pyproject.toml config tables."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tests._ci_contract import SERVICE_ROOT

STRICT = ConfigDict(extra="forbid", frozen=True)


class StatusDefault(BaseModel):
    model_config = STRICT
    informational: bool = False
    target: str | None = None
    threshold: str | None = None


class Component(BaseModel):
    model_config = STRICT
    component_id: str = ""
    carryforward: bool = False
    flag_regexes: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


class Codecov(BaseModel):
    """codecov.yml parsed once; unknown tables are a construction refusal."""

    model_config = STRICT
    path: Path
    project: StatusDefault
    patch: StatusDefault
    individual_components_is_list: bool = False
    components: Mapping[str, Component] = Field(default_factory=dict)
    carryforward_sites: tuple[tuple[str, ...], ...] = ()
    flag_carryforward: bool = False
    nightly_joined: bool | None = None

    @property
    def component_names(self) -> frozenset[str]:
        return frozenset(self.components)


class CoverageRun(BaseModel):
    model_config = STRICT
    core: str
    branch: bool
    source_pkgs: frozenset[str]
    relative_files: bool
    parallel: bool
    dynamic_context: bool


class RuffConfig(BaseModel):
    model_config = STRICT
    ignored: frozenset[str]
    known_first_party: frozenset[str]


class Pyproject(BaseModel):
    """The two ruff/coverage tables the CI contracts read, modelled strictly."""

    model_config = STRICT
    path: Path
    coverage_run: CoverageRun
    ruff: RuffConfig

    @field_validator("coverage_run")
    def source_pkgs_nonempty(cls, value: CoverageRun) -> CoverageRun:
        if not value.source_pkgs:
            raise ValueError("source_pkgs must not be empty")
        return value


def parse_codecov(path: Path) -> Codecov:
    """Parse codecov.yml into one strict object; never let rules reread its text."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is not a mapping")
    coverage = raw.get("coverage")
    status = coverage.get("status") if isinstance(coverage, dict) else None
    status = status if isinstance(status, dict) else {}
    project = _status_default(status.get("project"))
    patch = _status_default(status.get("patch"))
    management = raw.get("component_management")
    management = management if isinstance(management, dict) else {}
    raw_components = management.get("individual_components")
    components = {}
    is_list = isinstance(raw_components, list)
    if isinstance(raw_components, list):
        for entry in raw_components:
            component = _component(entry)
            components[component.component_id] = component
    flag_management = raw.get("flag_management")
    flag_management = flag_management if isinstance(flag_management, dict) else {}
    rules = flag_management.get("default_rules")
    rules = rules if isinstance(rules, dict) else {}
    flags = raw.get("flags")
    flags = flags if isinstance(flags, dict) else {}
    nightly = flags.get("nightly")
    nightly = nightly if isinstance(nightly, dict) else {}
    return Codecov(
        path=path,
        project=project,
        patch=patch,
        individual_components_is_list=is_list,
        components=components,
        carryforward_sites=_carryforward_sites(raw),
        flag_carryforward=rules.get("carryforward") is True,
        nightly_joined=nightly.get("joined") if isinstance(nightly.get("joined"), bool) else None,
    )


def _status_default(raw: object) -> StatusDefault:
    table = raw if isinstance(raw, dict) else {}
    default = table.get("default")
    default = default if isinstance(default, dict) else {}
    return StatusDefault(
        informational=default.get("informational") is True,
        target=None if default.get("target") is None else str(default["target"]),
        threshold=None if default.get("threshold") is None else str(default["threshold"]),
    )


def _component(raw: object) -> Component:
    table = raw if isinstance(raw, dict) else {}
    return Component(
        component_id=str(table.get("component_id", "")),
        carryforward=table.get("carryforward") is True,
        flag_regexes=tuple(str(item) for item in table.get("flag_regexes") or ()),
        paths=tuple(str(item) for item in table.get("paths") or ()),
    )


def _carryforward_sites(node: object, trail: tuple[str, ...] = ()) -> tuple[tuple[str, ...], ...]:
    if isinstance(node, dict):
        own = ((*trail, "carryforward"),) if "carryforward" in node else ()
        nested = tuple(
            site for key, value in node.items() for site in _carryforward_sites(value, (*trail, str(key)))
        )
        return (*own, *nested)
    if isinstance(node, list):
        return tuple(
            site for i, value in enumerate(node) for site in _carryforward_sites(value, (*trail, str(i)))
        )
    return ()


def parse_pyproject(path: Path) -> Pyproject:
    """Parse the coverage and ruff tables through tomllib, once."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    tool = raw.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    run = tool.get("coverage")
    run = run.get("run") if isinstance(run, dict) else None
    if not isinstance(run, dict):
        raise ValueError(f"{path.name} has no [tool.coverage.run]")
    ruff = tool.get("ruff")
    ruff = ruff if isinstance(ruff, dict) else {}
    lint = ruff.get("lint")
    lint = lint if isinstance(lint, dict) else {}
    isort = lint.get("isort")
    isort = isort if isinstance(isort, dict) else {}
    return Pyproject(
        path=path,
        coverage_run=CoverageRun(
            core=str(run.get("core", "")),
            branch=run.get("branch") is True,
            source_pkgs=frozenset(str(item) for item in run.get("source_pkgs") or ()),
            relative_files=run.get("relative_files") is True,
            parallel=run.get("parallel") is True,
            dynamic_context=run.get("dynamic_context") is True,
        ),
        ruff=RuffConfig(
            ignored=frozenset(str(item) for item in lint.get("ignore") or ()),
            known_first_party=frozenset(str(item) for item in isort.get("known-first-party") or ()),
        ),
    )


CODECOV = parse_codecov(SERVICE_ROOT / "codecov.yml")
PYPROJECT = parse_pyproject(SERVICE_ROOT / "pyproject.toml")
