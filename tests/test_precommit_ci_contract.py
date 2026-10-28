"""The pre-commit config is the single CI enforcement surface.

The shared paths, constants and config parsing live in tests/_ci_contract.py
(#476); this module keeps the dev-extra and pin-agreement gates of the
pre-commit surface itself.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib

from tests._ci_contract import (
    _HOOK_MODULE,
    CONFIG_PATH,
    SERVICE_ROOT,
    _entry,
    _hook_blocks,
)

# A syntactically valid config carrying a second, quoted jscpd hook that would pass a lax threshold. Nothing
# distinguishes it from the real config to a reader of the raw text except the quotes.
_FIXTURE_FILES = r"^(docker/browser-crawler/)?.*\.py$"


def _fixture_hook(hook_id: str, name: str, entry: str, *, quoted: bool = False) -> str:
    """One hook block of the fixture config. The hooks differ only in id, name,
    entry and whether the id is quoted -- the whole defect the fixture exists to
    express -- so the four shared lines are written once.
    """
    rendered_id = f'"{hook_id}"' if quoted else hook_id
    return (
        f"      - id: {rendered_id}\n"
        f"        name: {name}\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        f"        files: {_FIXTURE_FILES}\n"
        "        pass_filenames: false\n"
        "        stages: [pre-commit]\n"
    )


QUOTED_SECOND_JSCPD_HOOK = (
    "repos:\n"
    "  - repo: local\n"
    "    hooks:\n"
    + _fixture_hook("jscpd", "jscpd", ".venv/bin/python -m scripts.jscpd_gate all")
    + _fixture_hook(
        "jscpd-legacy",
        "jscpd legacy",
        "jscpd --threshold 40 app tests || true",
        quoted=True,
    )
)


def _dev_extra() -> list[str]:
    """The [project.optional-dependencies].dev entries of pyproject.toml."""
    document = tomllib.loads((SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document.get("project")
    assert isinstance(project, dict), "pyproject.toml must declare [project]"
    extras = project.get("optional-dependencies")
    assert isinstance(extras, dict) and "dev" in extras, "pyproject.toml must declare a dev extra"
    dev = extras["dev"]
    assert isinstance(dev, list) and dev, "the dev extra must be a non-empty list"
    return [str(entry) for entry in dev]


def _distribution_name(requirement: str) -> str:
    """The distribution a requirement line installs, before any pin or marker."""
    return re.split(r"[=<>!~;@\[ ]", requirement.strip(), maxsplit=1)[0]


def test_every_hook_python_module_is_importable_from_the_dev_extra() -> None:
    # Each hook module resolves against the dev extra's declarations and the
    # running interpreter, never against PATH.
    config = CONFIG_PATH.read_text(encoding="utf-8")
    modules = sorted(
        {match for _, block in _hook_blocks(config) for match in _HOOK_MODULE.findall(_entry(block))}
    )
    assert modules, "the hook surface must declare at least one `-m <module>` command"
    dev_names = {_distribution_name(entry).lower().replace("_", "-") for entry in _dev_extra()}
    unresolved: list[str] = []
    for module in modules:
        if module.startswith("scripts."):
            continue  # repo-local, installed by -e .
        if importlib.util.find_spec(module) is None:
            unresolved.append(f"{module}: not importable from the running dev venv")
        elif _distribution_name(module).lower() not in dev_names:
            unresolved.append(f"{module}: importable here but not declared in pyproject's [dev] extra")
    assert not unresolved, (
        "pre-commit hook modules the dev extra does not install:\n  "
        + "\n  ".join(unresolved)
        + "\nCI builds the hook interpreter from `-e .[dev]` alone (ci.yml enforcement "
        "job), so a module outside the extra fails the hook -- and the enforcement job -- "
        "on a clean checkout. Declare it in the dev extra or the hook cannot run in CI."
    )


def test_pins_shared_between_the_dev_extra_and_requirements_dev_agree() -> None:
    # requirements-dev.txt is not a CI source, so a divergent pin there is a
    # version CI never runs: both declarations of one tool must agree.
    dev_pins = {}
    for entry in _dev_extra():
        match = re.fullmatch(r"([\w.-]+)==([\w.]+)", entry.strip())
        if match:
            dev_pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    assert dev_pins, "the dev extra must pin its gate tools"
    req_text = (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    drift = []
    for line in req_text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"([\w.-]+)==([\w.]+)", stripped)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        if name in dev_pins and dev_pins[name] != match.group(2):
            drift.append(
                f"{name}: dev extra pins {dev_pins[name]}, requirements-dev.txt pins {match.group(2)}"
            )
    assert not drift, "pinned tool versions drift between the two declarations:\n  " + "\n  ".join(drift)
