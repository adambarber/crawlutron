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


def test_inventory_is_deterministic():
    """Two runs of main produce byte-identical output."""
    _, out1 = _run("text")
    _, out2 = _run("text")
    assert out1 == out2


def test_inventory_total_is_stable_and_positive():
    """The reported total is a stable positive integer across runs."""
    _, out1 = _run("json")
    total1 = json.loads(out1)["total"]
    assert isinstance(total1, int) and total1 > 0
    _, out2 = _run("json")
    assert json.loads(out2)["total"] == total1


def test_inventory_has_no_phantom_bare_node_ids():
    """No bare file::test id shadows a real file::Class::test id."""
    _, output = _run("json")
    ids = {t["node_id"] for group in json.loads(output)["groups"] for t in group["tests"]}
    for node_id in ids:
        parts = node_id.split("::")
        if len(parts) == 3:
            bare = "::".join((parts[0], parts[2]))
            assert bare not in ids, f"phantom bare node id {bare!r} shadows {node_id!r}"
