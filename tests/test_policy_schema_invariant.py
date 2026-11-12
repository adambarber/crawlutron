"""The policy-schema law holds in history and in the gate (#561).

Two pins, one law: any feat!-shaped commit touching crawl_policy.py in the recorded history bumped
POLICY_SCHEMA_VERSION, and the commit-msg gate refuses a synthetic breaking-feat touch without a bump.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = "crawlutron/models/crawl_policy.py"
_DECL = 'POLICY_SCHEMA_VERSION = "'


def _git(*args: str) -> str:
    out = subprocess.run(
        ("/usr/bin/git", *args), cwd=SERVICE_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout


def _tree_decl(rev: str) -> str | None:
    blob = _git("show", f"{rev}:{POLICY_PATH}")
    for line in blob.splitlines():
        stripped = line.strip()
        if stripped.startswith(_DECL):
            return stripped[len(_DECL) :].strip('"')
    return None


def test_history_feats_touching_policy_bumped_the_schema_version():
    shas = _git(
        "log",
        "--diff-filter=M",
        "--pretty=%H",
        "-E",
        "--grep=^feat!|^feat\\([^)]*\\)!:",
        "--extended-regexp",
        POLICY_PATH,
    ).split()
    bumps = []
    for sha in shas:
        current, parent = _tree_decl(sha), _tree_decl(f"{sha}^")
        assert current is not None and parent is not None
        assert current != parent, f"feat! {sha} touched {POLICY_PATH} without a schema bump"
        bumps.append((sha, parent, current))
    assert bumps == []


@pytest.mark.parametrize(
    ("bumped", "expected"),
    [(False, 1), (True, 0)],
    ids=["refused-without-bump", "accepted-with-bump"],
)
def test_the_gate_law(tmp_path, monkeypatch, bumped, expected):
    import scripts.commit_citation_gate as gate

    monkeypatch.setattr(
        gate,
        "_diff_paths",
        lambda: ["crawlutron/models/crawl_policy.py", "msg.txt"],
    )
    monkeypatch.setattr(gate, "_schema_bumped", lambda: bumped)
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text("feat(policy)!: break the wire\n\nCloses #561\n", encoding="utf-8")
    assert gate.main([str(msg_file)]) == expected
