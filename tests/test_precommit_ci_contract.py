"""The pre-commit config is the single CI enforcement surface."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "ci.yml"
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
ENFORCEMENT_COMMAND = "pre-commit run --all-files --config docker/browser-crawler/.pre-commit-config.yaml"
REPORTING_JOBS = {"lint", "typecheck", "test-fast"}
# Every file the gates read to decide what to enforce. A commit that edits one of
# them must run the enforcement: the config that decides what is checked cannot
# itself be the one change that skips the check.
ENFORCEMENT_INPUTS = (
    "app/settings.py",
    "tests/test_jscpd_gate.py",
    "scripts/module_graph.py",
    ".pre-commit-config.yaml",
    ".jscpd.json",
    "jscpd-expectations.json",
    "pyrightconfig.json",
    "mypy.ini",
    "pytest.ini",
    "pyproject.toml",
    "requirements-dev.txt",
    ".github/workflows/ci.yml",
)
# The single trigger pattern every hook carries. Optional leading segment so the
# same pattern matches nested under the parent repo and at the repository root
# once the service is extracted.
ENFORCEMENT_TRIGGER_PATTERN = (
    r"^(docker/browser-crawler/)?(.*\.py|\.pre-commit-config\.yaml|\.jscpd\.json"
    r"|jscpd-expectations\.json|pyrightconfig\.json|mypy\.ini|pytest\.ini"
    r"|pyproject\.toml|requirements-dev\.txt|requirements\.txt|\.github/workflows/.*\.ya?ml)$"
)
# A syntactically valid config carrying a second, quoted jscpd hook that would
# pass a lax threshold. Nothing distinguishes it from the real config to a
# reader of the raw text except the quotes.
QUOTED_SECOND_JSCPD_HOOK = """\
repos:
  - repo: local
    hooks:
      - id: jscpd
        name: jscpd
        entry: .venv/bin/python tests/_jscpd_gate.py all
        language: system
        files: ^(docker/browser-crawler/)?.*\\.py$
        pass_filenames: false
        stages: [pre-commit]
      - id: "jscpd-legacy"
        name: jscpd legacy
        entry: jscpd --threshold 40 app tests || true
        language: system
        files: ^(docker/browser-crawler/)?.*\\.py$
        pass_filenames: false
        stages: [pre-commit]
"""


def _hook_blocks(config: str) -> list[tuple[str, dict]]:
    """Every hook in the config, as `(id, hook)` pairs read by a YAML parser.

    This used to be a regex over the raw text anchored to exactly six spaces of
    indentation, which meant a hook id written `- id: "jscpd-legacy"` was
    invisible here: the contract could report one jscpd gate while the file
    declared two. pre-commit reads ids as YAML values, so this test reads them
    the same way. Quoting, indentation and key order are no longer ways to be
    invisible.
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


def _field(block: dict, name: str) -> str:
    assert name in block, f"hook is missing {name}"
    return str(block[name])


def _entry(block: dict) -> str:
    """The command pre-commit will execute, with folded scalars already joined.

    The assertion that used to be here (that `entry:` is a `>-` block) was a
    proxy for the thing that matters: pre-commit runs the entry as one command,
    so a literal block would smuggle newlines into it. That is asserted directly.
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


def _required_needs(workflow: str) -> set[str]:
    required = re.search(r"^  required:\n(?P<body>.*?)(?=^\S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert required is not None, "CI must declare the required aggregate job"
    needs = re.search(r"^\s+needs: \[(?P<jobs>[^\]]+)\]", required.group("body"), re.MULTILINE)
    assert needs is not None, "the required aggregate must declare needs"
    return {job.strip() for job in needs.group("jobs").split(",")}


def test_ci_runs_every_precommit_hook_through_one_surface() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    hooks = _hook_blocks(config)
    assert hooks

    jscpd_id, jscpd = _require_exactly_one_jscpd_gate(config)
    assert jscpd_id == "jscpd"
    assert "jscpd-app" not in config
    assert "jscpd-tests" not in config
    assert not [hook_id for hook_id, _ in hooks if hook_id in {"jscpd-app", "jscpd-tests"}]

    assert all("pre-push" not in (block.get("stages") or []) for _, block in hooks)
    assert all(block.get("stages") == ["pre-commit"] for _, block in hooks)

    entry = _entry(jscpd)
    assert ".venv/bin/python tests/_jscpd_gate.py all" in entry
    assert "--min-lines 8" in entry
    assert "--min-tokens 40" in entry
    assert "--threshold 0" in entry
    assert _field(jscpd, "files") == ENFORCEMENT_TRIGGER_PATTERN

    expectations = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    assert all("profiles" not in pair for pair in expectations["pairs"])

    assert workflow.count(ENFORCEMENT_COMMAND) == 1
    assert "tests/_jscpd_gate.py" not in workflow
    required = _required_needs(workflow)
    assert "enforcement" in required
    assert required - {"enforcement"} <= REPORTING_JOBS
    assert "pre-commit==4.6.2" in (SERVICE_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


def test_quoted_hook_id_is_visible_to_this_contract() -> None:
    """A hook id is a YAML value, not a line of text.

    The parser used to be `^      - id: ([A-Za-z0-9_-]+)\\s*$`, so a hook
    declared `- id: "jscpd-legacy"` did not exist as far as this contract was
    concerned and a second, laxer duplication gate could be added undetected.
    """
    hooks = _hook_blocks(QUOTED_SECOND_JSCPD_HOOK)
    assert [hook_id for hook_id, _ in hooks] == ["jscpd", "jscpd-legacy"]


def test_a_second_quoted_jscpd_hook_is_rejected() -> None:
    with pytest.raises(AssertionError):
        _require_exactly_one_jscpd_gate(QUOTED_SECOND_JSCPD_HOOK)


def test_every_gate_fires_on_every_enforcement_input() -> None:
    """No gate's own configuration can be edited without that gate running.

    The `files:` pattern is a pre-commit trigger, so a config that no pattern
    matches is a config nothing checks. `.jscpd.json` was the live instance: every
    hook matched only `.*\\.py`, so the commit that added an ignore glob to hide a
    clone ran no hook at all.
    """
    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    assert hooks
    for hook_id, block in hooks:
        pattern = _field(block, "files")
        for name in ENFORCEMENT_INPUTS:
            # Both layouts: nested under the parent repo, and at the repository
            # root once the service is extracted.
            for candidate in (f"docker/browser-crawler/{name}", name):
                assert re.search(pattern, candidate), f"hook {hook_id} does not fire on {candidate}"
