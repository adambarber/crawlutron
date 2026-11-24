"""Residual contract tests for weekly.yml not covered by the typed workflow rules."""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from pathlib import Path

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "weekly.yml"
PYPROJECT_PATH = SERVICE_ROOT / "pyproject.toml"


def workflow_document() -> dict[object, object]:
    """Load and parse the weekly workflow."""
    import yaml

    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "weekly.yml is not a workflow"
    return document


def test_weekly_workflow_exists() -> None:
    """weekly.yml must exist."""
    assert WORKFLOW_PATH.exists(), "weekly.yml does not exist"


def test_weekly_import_check_matches_source_pkgs() -> None:
    """weekly.yml lowest-direct probe must import exactly the source_pkgs from pyproject.toml."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    resolution_job = jobs.get("lowest-direct-resolution")
    assert isinstance(resolution_job, dict), "lowest-direct-resolution job required"

    steps = resolution_job.get("steps")
    assert isinstance(steps, list), "lowest-direct-resolution job must have steps"

    import_step = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        if "import" in run and ("crawlutron" in run or "admission" in run or "safe_url" in run):
            import_step = step
            break

    assert import_step is not None, "import verification step required"

    import re

    expected_packages = _get_source_pkgs_from_pyproject()

    run_cmd = import_step.get("run", "")
    match = re.search(r"packages\s*=\s*\[(.*?)\]", run_cmd, re.DOTALL)
    assert match, f"could not find packages list in import step: {run_cmd}"

    packages_str = match.group(1)
    found_packages = re.findall(r"['\"]([^'\"]+)['\"]", packages_str)
    found_packages = [pkg.replace("-", "_") for pkg in found_packages]

    assert set(found_packages) == set(expected_packages), (
        f"workflow import list {sorted(found_packages)} does not match "
        f"pyproject.toml source_pkgs {sorted(expected_packages)}"
    )


def _get_source_pkgs_from_pyproject() -> list[str]:
    """Derive the expected source_pkgs from pyproject.toml."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    source_pkgs = data.get("tool", {}).get("coverage", {}).get("run", {}).get("source_pkgs", [])
    assert isinstance(source_pkgs, list), "source_pkgs must be a list"
    return [str(pkg) for pkg in source_pkgs]


def test_mutmut_in_dev_dependencies() -> None:
    """pyproject.toml must include mutmut==3.2.0 in [project.optional-dependencies] dev."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)

    dev_deps = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert isinstance(dev_deps, list), "dev dependencies must be a list"

    mutmut_found = any(dep.startswith("mutmut==3.2.0") for dep in dev_deps)
    assert mutmut_found, f"mutmut==3.2.0 not found in dev dependencies: {dev_deps}"


def _mutmut_cli_surface() -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Derive commands and flags from the pinned mutmut click CLI."""
    from mutmut.__main__ import cli

    commands = frozenset(cli.commands)
    flags = {
        name: frozenset(
            option
            for param in command.params
            for option in (*param.opts, *param.secondary_opts)
            if option.startswith("-")
        )
        | {"--help"}
        for name, command in cli.commands.items()
    }
    return commands, flags


def _mutmut_argument_lists(document: object) -> Iterator[list[str]]:
    """Yield argument vectors for every mutmut call in a workflow document."""
    if not isinstance(document, dict):
        return
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run", "")
            if not isinstance(run, str):
                continue
            for line in run.replace("\\\n", " ").splitlines():
                if "mutmut" not in line:
                    continue
                tokens = shlex.split(line, comments=True)
                for position, token in enumerate(tokens):
                    if Path(token).name == "mutmut":
                        yield tokens[position + 1 :]


def test_weekly_invokes_only_flags_from_the_pinned_mutmut_cli() -> None:
    """No mutmut call in weekly.yml may use a flag or command the pin lacks."""
    commands, flags = _mutmut_cli_surface()
    for args in _mutmut_argument_lists(workflow_document()):
        command = next((arg for arg in args if not arg.startswith("-")), None)
        assert command in commands, f"unknown mutmut command {command!r}; supported: {sorted(commands)}"
        unsupported = [arg for arg in args if arg.startswith("-") and arg.split("=")[0] not in flags[command]]
        assert not unsupported, f"flags absent from pinned mutmut {command}: {unsupported}"


def test_mutmut_flag_guard_names_a_reintroduced_two_x_flag() -> None:
    """Bite: a planted 2.x flag fails the pinned-CLI guard."""
    planted = {
        "jobs": {"weekly": {"steps": [{"run": ".venv/bin/mutmut run --paths-to-mutate tests/invariants"}]}}
    }
    commands, flags = _mutmut_cli_surface()
    assert list(_mutmut_argument_lists(planted)) == [["run", "--paths-to-mutate", "tests/invariants"]]
    assert "run" in commands and "--paths-to-mutate" not in flags["run"]


def test_weekly_has_no_broken_mutation_job() -> None:
    """The never-working mutmut 2.x job must stay removed until the pin supports it."""
    jobs = workflow_document().get("jobs")
    assert isinstance(jobs, dict)
    assert "mutation-testing" not in jobs
