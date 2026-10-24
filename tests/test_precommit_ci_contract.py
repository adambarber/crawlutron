"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]


CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"


# The developer Makefile.
MAKEFILE_PATH = SERVICE_ROOT / "Makefile"


WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"


# The config as the workflow must name it.
WORKFLOW_ROOT_CONFIG = CONFIG_PATH.relative_to(SERVICE_ROOT).as_posix()


ENFORCEMENT_COMMAND = f"pre-commit run --all-files --config {WORKFLOW_ROOT_CONFIG}"


# Every job that reports a result the aggregate must depend on.
REPORTING_JOBS = {"enforcement", "test-fast", "test-browser"}


# GitHub's template form for an errexit shell. The required aggregate is a chain of `test` lines, so its exit
# code is its last line's without errexit.
ERREXIT_SHELL = "bash -e {0}"


# Every file the gates read to decide what to enforce. A commit that edits one of them must run the
# enforcement: the config that decides what is checked cannot itself be the one change that skips the check.
ENFORCEMENT_INPUTS = (
    "app/config/settings.py",
    "tests/_jscpd_gate_helpers.py",
    # The duplication gate's own implementation.
    "scripts/jscpd_gate.py",
    "scripts/module_graph.py",
    # The Makefile is a gate entry point: `make lint` and `make typecheck` run hooks out of the config.
    "Makefile",
    # The gate entry point itself.
    "scripts/gate",
    ".pre-commit-config.yaml",
    ".jscpd.json",
    "pyrightconfig.json",
    "mypy.ini",
    "pytest.ini",
    "pyproject.toml",
    "requirements-dev.txt",
    ".github/workflows/ci.yml",
)


# The single trigger pattern every hook shares, held as one YAML anchor (`*service_trigger`) in
# .pre-commit-config.yaml so the copies cannot drift.
ENFORCEMENT_TRIGGER_PATTERN = r"^(docker/browser-crawler/|(?!docker/))"


# The committed gate entry point every hook resolves to, and the shape of the entry that resolves it.
GATE_HELPER = SERVICE_ROOT / "scripts" / "gate"


GATE_HELPER_PATH = "scripts/gate"


GATE_ENTRY_END = "' gate "


# The one-line `bash -c` prologue every entry must open with, pinned byte for byte so the five copies cannot
# drift.
GATE_ENTRY_PROLOGUE = (
    "bash -c 'for d in "
    '"$(git rev-parse --show-toplevel)/docker/browser-crawler" '
    '"$(git rev-parse --show-toplevel)"; '
    f'do [ -x "$d/{GATE_HELPER_PATH}" ] && exec bash "$d/{GATE_HELPER_PATH}" "$@"; '
    f'done; echo "{GATE_HELPER_PATH} not found in either layout" >&2; exit 1\''
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


def _hook_blocks(config: str) -> list[tuple[str, dict]]:
    """Every hook in the config as `(id, hook)` pairs, read like pre-commit reads
    them (YAML, not a raw-text regex) -- a quoted id was once invisible here,
    reporting one jscpd gate where the file declared two. Formatting hides it not.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    hooks: list[tuple[str, dict]] = []
    for repo in document.get("repos") or []:
        assert isinstance(repo, dict), "each repos entry must be a mapping"
        for hook in repo.get("hooks") or []:
            assert isinstance(hook, dict), "each hook must be a mapping"
            hook_id = hook.get("id")
            assert isinstance(hook_id, str) and hook_id, f"hook is missing a string id: {hook!r}"
            hooks.append((hook_id, hook))
    return hooks


PRE_COMMIT = "pre-commit"


def _assert_no_config_exclude(config: str) -> None:
    """`exclude: .*` disables every hook -- all files excluded, all hooks Skipped,
    exit 0 -- so the contract runs the real binary against a synthetic config that
    replaces the real hooks with `exit 1`, proving a bypass would vanish.
    """
    document = yaml.safe_load(config)
    assert isinstance(document, dict), "the pre-commit config must be a mapping"
    exclude = document.get("exclude")
    if exclude is None:
        return
    assert isinstance(exclude, str), "the pre-commit config exclude must be a string pattern"
    # `.*` matches every file — total disable. `*.py` matches every Python file — disables every Python-scoped
    # hook.
    if exclude in (".*", "*.py", ".*.py$"):
        raise ValueError(
            f"config exclude={exclude!r} disables every hook; "
            "only safe repo dirs (venv, cache, git) may be excluded"
        )


def _field(block: dict[str, object], name: str) -> str:
    assert name in block, f"hook is missing {name}"
    return str(block[name])


def _entry(block: dict[str, object]) -> str:
    """The command pre-commit executes, with folded scalars joined. The old `>-`
    assertion was a proxy for the real rule: pre-commit runs the entry as ONE
    command, so a literal block would smuggle newlines in.
    """
    entry = block.get("entry")
    assert isinstance(entry, str) and entry.strip(), "hook is missing an entry"
    assert "\n" not in entry, "entry must be a folded scalar, not a literal one"
    return entry


def _require_exactly_one_jscpd_gate(config: str) -> tuple[str, dict]:
    """One duplication gate, by id, whatever the file looks like."""
    hooks = _hook_blocks(config)
    jscpd_hooks = [(hook_id, hook) for hook_id, hook in hooks if hook_id.split("-")[0] == "jscpd"]
    found = [hook_id for hook_id, _ in jscpd_hooks]
    assert len(jscpd_hooks) == 1, f"exactly one jscpd gate is required, found {found}"
    jscpd_id, jscpd = jscpd_hooks[0]
    assert jscpd_id == "jscpd", f"the only jscpd gate must be the whole-service one, found {jscpd_id!r}"
    return jscpd_id, jscpd


# A hook module absent from the dev extra is a ModuleNotFoundError on a clean
# checkout -- vulture's defect, pinned only in requirements-dev.txt (no CI source).
_HOOK_MODULE = re.compile(r"\.venv/bin/python -m ([\w.]+)")


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
