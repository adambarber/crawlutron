"""Stale CI pins, wrong version comments, and the moving runner label are refused."""

from __future__ import annotations

import re

import yaml

from tests._ci_contract import PREPARE_ACTION_PATH, SERVICE_ROOT, WORKFLOW_PATH

RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"
PACKAGING_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "packaging.yml"

# The four converged pins #1067 landed: action name -> (full SHA, version comment).
# A legitimate upgrade edits this table; a SHA slipped past review reds here.
CONVERGED_PINS = {
    "actions/setup-python": (
        "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
        "v7.1.0",
    ),
    "astral-sh/setup-uv": (
        "68559e99cc807c1eb7e9238fd43a15bba460eec2",
        "v10.3.3",
    ),
    "actions/cache": (
        "caa296126883cff596d87d8935842f9db880ef25",
        "v5",
    ),
    "gitleaks/gitleaks-action": (
        "e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
        "v3.0.0",
    ),
}

# Stale SHAs #1067 removed: setup-python v5, setup-uv v5, cache v4, gitleaks v2.
STALE_SHAS = (
    "a26af69be951a213d495a4c3e4e4022e16d87065",
    "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
    "0057852bfaa89a56745cba8c7296529d2fc39830",
    "ff98106e4c7b2bc287b24eaf42907196329070c7",
)

CI_SURFACE = (
    WORKFLOW_PATH,
    RELEASE_WORKFLOW_PATH,
    PACKAGING_WORKFLOW_PATH,
    PREPARE_ACTION_PATH,
)

WORKFLOW_FILES = (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, PACKAGING_WORKFLOW_PATH)

_USES_LINE = re.compile(r"uses:\s*(\S+?)(?:\s+#\s*(\S+))?\s*$")


def _surface_texts() -> list[tuple[str, str]]:
    """Each file on the CI surface as (file name, text)."""
    return [(path.name, path.read_text(encoding="utf-8")) for path in CI_SURFACE]


def _uses_refs(text: str) -> list[tuple[str, str, str]]:
    """Every ``uses:`` line as (action_name, sha, version_comment). YAML strips
    the inline comment, so this reads raw text instead."""
    found: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        match = _USES_LINE.search(line)
        if not match:
            continue
        ref, comment = match.group(1), match.group(2)
        name, _, sha = ref.partition("@")
        found.append((name.strip(), sha.strip(), (comment or "").strip()))
    return found


def test_no_ubuntu_latest_runner_label() -> None:
    """ubuntu-latest migrates; no job may ride the moving label."""
    for name, text in _surface_texts():
        assert "ubuntu-latest" not in text, f"{name} still uses ubuntu-latest"


def test_no_stale_action_pin() -> None:
    """Every `uses:` keeps the converged SHA; a stale pin must not return."""
    for name, text in _surface_texts():
        for sha in STALE_SHAS:
            assert f"@{sha}" not in text, f"{name} re-pins the stale SHA {sha}"


def test_every_job_pins_ubuntu_24_04() -> None:
    """Every job names ubuntu-24.04 as a stated fact."""
    for path in WORKFLOW_FILES:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a workflow mapping"
        jobs = document.get("jobs")
        assert isinstance(jobs, dict), f"{path.name} declares no jobs"
        for name, job in jobs.items():
            assert isinstance(job, dict), f"job {name} in {path.name} is not a mapping"
            # Jobs that call reusable workflows (`uses:`) run in the called
            # workflow's runner; they carry no `runs-on` of their own.
            if job.get("uses"):
                continue
            assert job.get("runs-on") == "ubuntu-24.04", (
                f"job {name} in {path.name} does not pin ubuntu-24.04"
            )


def test_uses_sha_matches_version_comment() -> None:
    """Each converged action pins its expected SHA *and* version comment.

    A v7 SHA with a stale # v6 comment (or the reverse) reds here, so a wrong
    pin cannot look reviewed. A legitimate upgrade edits this table deliberately.
    """
    for path in CI_SURFACE:
        for name, sha, comment in _uses_refs(path.read_text(encoding="utf-8")):
            if name not in CONVERGED_PINS:
                continue
            expected_sha, expected_comment = CONVERGED_PINS[name]
            assert sha == expected_sha, f"{path.name}: {name} pins {sha}, expected {expected_sha}"
            assert comment == expected_comment, (
                f"{path.name}: {name} carries '# {comment}', expected '# {expected_comment}'"
            )
