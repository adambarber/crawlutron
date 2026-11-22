"""Browser job tests."""

from __future__ import annotations

from tests._ci_contract import WORKFLOW_PATH

RUN_TESTS_ACTION_PATH = WORKFLOW_PATH.parent.parent / "actions" / "run-tests" / "action.yml"


def test_browser_job_installs_pinned_fpgen_model_before_tests() -> None:
    """Warm camoufox cache skips fetch; model installer must run first."""
    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    assert "ensure_fpgen_model" in action, "the door must install the fpgen model"
    assert "camoufox.fpgen_model" in action, "the door must use camoufox.fpgen_model"
    assert "python -m fpgen fetch" not in action, "the door must not call fpgen fetch directly"
    fpgen_index = action.index("ensure_fpgen_model")
    pytest_index = action.index("--junitxml=")
    assert fpgen_index < pytest_index, "the model must be installed before pytest"
