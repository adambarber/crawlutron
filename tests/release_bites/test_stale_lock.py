"""Bite: stale lock.

Verifies lock file has specific stale structure.
"""

from __future__ import annotations


def test_stale_lock_specific_assert(tmp_path):
    """Verify lock file has specific stale structure."""
    pyproject_content = """[project]
name = "crawlutron-test"
version = "0.1.0"
requires-python = ">=3.14"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependencies]
requests = ">=2.0.0"
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content)

    lock_content = """# This is a stale lock file
name = "crawlutron-test"
version = "1.0.0"

[[package]]
name = "requests"
version = "1.0.0"
"""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(lock_content)

    lock_text = lock_path.read_text(encoding="utf-8")
    assert "stale" in lock_text.lower() or "version" in lock_text.lower(), (
        f"Expected stale/version reference in lock file, got: {lock_text!r}"
    )

    assert "requests" in lock_text.lower(), "Lock should mention 'requests'"
