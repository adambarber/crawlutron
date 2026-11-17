"""Smoke tests for the test inventory tool."""

import contextlib
import io
import json
import sys

from scripts.test_inventory import main


def _run(fmt: str) -> tuple[int, str]:
    """Run the inventory tool for one format, returning (exit code, stdout)."""
    buffer = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["test_inventory", "--format", fmt]
    try:
        with contextlib.redirect_stdout(buffer):
            code = main()
    finally:
        sys.argv = old_argv
    return code, buffer.getvalue()


def test_inventory_text_reports_a_total():
    """The text mode exits zero and reports a total test count."""
    code, output = _run("text")
    assert code == 0
    assert "total tests:" in output


def test_inventory_json_emits_a_total():
    """The JSON mode exits zero and emits a payload with an integer total."""
    code, output = _run("json")
    assert code == 0
    payload = json.loads(output)
    assert isinstance(payload["total"], int)
    assert payload["total"] > 0
