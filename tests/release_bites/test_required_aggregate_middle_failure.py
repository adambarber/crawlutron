"""Bite: required-aggregate middle-failure.

Verifies aggregate config has specific middle-failure structure.
"""

from __future__ import annotations


def test_required_aggregate_middle_failure_specific_message(tmp_path):
    """Verify aggregate config has specific middle-failure structure."""
    aggregate_config_content = """[tool.release]
aggregates = [
    {name = "first-aggregate", if = "condition1"},
    {name = "middle-aggregate", if = "condition_middle", required = true},
    {name = "last-aggregate", if = "condition3"},
}

[tool.release.aggregates.middle-aggregate]
check = false
"""
    config_path = tmp_path / "release.toml"
    config_path.write_text(aggregate_config_content)

    aggregate_text = config_path.read_text(encoding="utf-8")

    assert "middle-aggregate" in aggregate_text, (
        f"Expected 'middle-aggregate' in aggregate config, got: {aggregate_text!r}"
    )
    assert "required = true" in aggregate_text, (
        f"Expected 'required = true' for middle-aggregate, got: {aggregate_text!r}"
    )
    assert "check = false" in aggregate_text, (
        f"Expected 'check = false' simulating middle failure, got: {aggregate_text!r}"
    )
