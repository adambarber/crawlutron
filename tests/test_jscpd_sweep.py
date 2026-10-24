"""The jscpd frontier sweep is a live tool, not a dead import.

It exists to measure floors below the enforced gate; these tests prove the
sweep runs, surfaces a clone, and reads the enforced floor, not a stale number.
"""

from __future__ import annotations

from app.models.jscpd_report import JscpdDuplicate
from scripts import jscpd_gate as gate
from scripts import jscpd_sweep as sweep


def _clone() -> JscpdDuplicate:
    return {
        "firstFile": {"name": "one.py", "start": 1, "end": 2, "tree": "app"},
        "secondFile": {"name": "two.py", "start": 1, "end": 2, "tree": "app"},
        "fragment": "def run():\n    return 1\n",
        "tokens": 8,
    }


def test_the_sweep_runs_over_the_roots_and_reports_a_clone(monkeypatch) -> None:
    """The sweep's scan path is wired to a report producer, so a clone surfaces."""
    clone = _clone()

    def fake_report(root: str, floor: tuple[int, int], **kwargs):
        return {"duplicates": [clone]}

    monkeypatch.setattr(gate, "jscpd_report", fake_report)

    results = sweep.sweep(2, 2, 20, 20)
    pairs = sweep._all(results[(2, 20)])
    assert len(pairs) == len(gate.PROFILE_PATHS["all"]), "one reported clone per scanned root"
    assert all(pair["fragment"] == clone["fragment"] for pair in pairs)
    assert sweep.classify(clone["fragment"]) == "logic"


def test_the_sweep_classifies_an_import_preamble_as_preamble() -> None:
    """The logic-vs-preamble distinction the floors table makes is real."""
    assert sweep.classify("import os\n\nimport sys\n") == "preamble"
    assert sweep.classify("from pathlib import Path\nfrom typing import Any\n") == "preamble"


def test_the_current_floor_section_reads_the_enforced_floor(monkeypatch, capsys) -> None:
    """The sweep's 'AT THE CURRENT FLOOR' header names the hook's 3/30, not a stale 4/39."""
    min_lines, min_tokens, _ = gate.configured_floor()

    def fake_report(root: str, floor: tuple[int, int], **kwargs):
        return {"duplicates": []}

    monkeypatch.setattr(gate, "jscpd_report", fake_report)
    monkeypatch.setattr("sys.argv", ["jscpd_sweep", "--min-lines", "2", "--max-lines", "4"])

    assert sweep.main() == 0
    out = capsys.readouterr().out
    assert f"AT THE CURRENT FLOOR ({min_lines}/{min_tokens})" in out
    assert "(4/39)" not in out
