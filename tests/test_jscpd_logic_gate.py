"""The 2/30 helper-surface logic gate fires closed on cross-file logic clones.

The 3/30 per-tree gate misses a two-line repetition between helper files; this
gate fails on cross-file clones whose fragment is LOGIC.
"""

from __future__ import annotations

from scripts import _jscpd_pair_resolution as resolver
from scripts import jscpd_logic_gate as gate


def _duplicate(first: str, second: str, fragment: str) -> dict:
    return {
        "firstFile": {"name": first, "start": 1, "end": 3},
        "secondFile": {"name": second, "start": 1, "end": 3},
        "fragment": fragment,
    }


def test_a_cross_file_logic_clone_fires_the_gate(monkeypatch, capsys) -> None:
    """Two helper files sharing a two-line logic block fail the gate."""
    clone = _duplicate("tests/_a.py", "tests/_b.py", "def run(a, b):\n    return a + b\n    \n")

    def fake_report(root, floor, **kwargs):
        return {"duplicates": [clone]}

    monkeypatch.setattr(resolver, "jscpd_report", fake_report)
    assert gate._run((2, 30)) == 1
    assert "tests/_a.py" in capsys.readouterr().err


def test_a_preamble_clone_does_not_fire(monkeypatch) -> None:
    """An import/boilerplate fragment between two helpers is not a logic finding."""
    preamble = _duplicate("tests/_a.py", "tests/_b.py", "import os\nimport sys\n")
    monkeypatch.setattr(resolver, "jscpd_report", lambda root, floor, **kw: {"duplicates": [preamble]})
    assert gate._run((2, 30)) == 0


def test_within_file_repetition_does_not_fire(monkeypatch) -> None:
    """A repetition inside one file is not cross-file helper sprawl."""
    same = _duplicate("tests/_a.py", "tests/_a.py", "def run(a, b):\n    return a + b\n    \n")
    monkeypatch.setattr(resolver, "jscpd_report", lambda root, floor, **kw: {"duplicates": [same]})
    assert gate._run((2, 30)) == 0


def test_the_helper_surface_is_declared() -> None:
    """The gate scans the helper namespace, not arbitrary test bodies."""
    surface = gate.helper_surface()
    assert "app" in surface
    assert "scripts" in surface
    assert any(name for name in surface if name.startswith("tests/_"))
    assert not any(name for name in surface if name.startswith("tests/invariants/"))


def test_a_real_two_line_logic_fragment_classifies_as_logic() -> None:
    """The classifier the gate leans on genuinely labels a two-line block logic."""
    from scripts.jscpd_sweep import classify

    assert classify("def run(a, b):\n    return a + b\n") == "logic"
    assert classify("import os\nimport sys\n") == "preamble"


def test_the_gate_is_a_precommit_hook() -> None:
    """The 2/30 sweep is wired into the gate stack, not an orphan script."""
    from tests.test_precommit_ci_contract import CONFIG_PATH, _hook_blocks

    hooks = _hook_blocks(CONFIG_PATH.read_text(encoding="utf-8"))
    ids = {hook_id for hook_id, _ in hooks}
    assert "helper-clone-sweep" in ids
