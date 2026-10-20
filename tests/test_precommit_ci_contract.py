"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.executables import GIT, require_executable, require_repo_tool

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


def _run_precommit_on_temp_config(temp_dir: Path, config_text: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL pre-commit binary against a synthetic config in a temp dir; the
    caller asserts on returncode. This is how the gate proves no `exclude` pattern
    disables every hook -- the binary must actually run something.
    """
    config_path = Path(temp_dir) / "test-pre-commit-config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    (Path(temp_dir) / "probe.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([require_executable(GIT), "init", "-q", str(temp_dir)], check=True, capture_output=True)
    subprocess.run(
        [require_executable(GIT), "-C", str(temp_dir), "add", "probe.py"], check=True, capture_output=True
    )
    return subprocess.run(
        [require_repo_tool(SERVICE_ROOT, PRE_COMMIT), "run", "--all-files", "--config", str(config_path)],
        cwd=temp_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


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


def _field(block: dict, name: str) -> str:
    assert name in block, f"hook is missing {name}"
    return str(block[name])


def _entry(block: dict) -> str:
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


def _synthetic_config(exclude: str | None, entry: str = "bash -c 'exit 1'") -> str:
    """A minimal local pre-commit config carrying one always-failing hook."""
    exclude_line = f"exclude: {exclude}\n" if exclude is not None else ""
    return (
        f"{exclude_line}"
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: synthetic\n"
        "        name: synthetic gate\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        files: '\\.py$'\n"
    )


def test_config_exclude_dot_star_makes_hooks_skipped(tmp_path) -> None:
    """REPRO: under `exclude: .*` a hook that runs `exit 1` reports Skipped and the
    binary exits 0, so the enforcement vanishes with nothing to trip on -- a root
    exclude is a bypass no execution catches, because hooks never run.
    """
    skipped = _run_precommit_on_temp_config(tmp_path, _synthetic_config(".*"))
    assert skipped.returncode == 0, f"skipped hooks exit 0:\n{skipped.stdout}"
    assert "Skipped" in skipped.stdout, f"the hook must be reported Skipped:\n{skipped.stdout}"


def test_config_without_exclude_runs_the_hook_and_fails(tmp_path) -> None:
    """The harness is real pre-commit: without exclude the hook fails."""
    completed = _run_precommit_on_temp_config(tmp_path, _synthetic_config(None))
    assert completed.returncode != 0, (
        f"the synthetic hook must fail when it actually runs:\nstdout:\n{completed.stdout}"
    )
    assert "exit code: 1" in completed.stdout, f"the hook did not run as expected:\n{completed.stdout}"


def test_the_gate_rejects_a_config_exclude_that_disables_every_hook() -> None:
    """Bites: `_assert_no_config_exclude` fires on `exclude: .*`."""
    with pytest.raises(ValueError, match="disables every hook"):
        _assert_no_config_exclude("repos: []\nexclude: .*\n")


def test_the_gate_accepts_a_config_without_a_root_exclude() -> None:
    _assert_no_config_exclude("repos: []\n")
