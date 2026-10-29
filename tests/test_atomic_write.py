"""atomic_write chokepoint contract: every disk write is crash-safe. One helper,
one audited mkstemp + fsync + os.replace dance, replacing the cookie jar's
hand-rolled copy and ContentCache's bare ``path.write_text``.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from crawlutron.safety.atomic_write import atomic_write_text


def _watch_fsync(monkeypatch, calls: list[int]) -> None:
    """Count ``os.fsync`` calls without dropping them, so durability is observable."""
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))


class TestAtomicWrite:
    def test_roundtrip_exact_content(self, tmp_path):
        target = tmp_path / "a.json"
        atomic_write_text(target, '{"k": 1}')
        assert target.read_text() == '{"k": 1}'

    def test_no_temp_files_left_behind(self, tmp_path):
        atomic_write_text(tmp_path / "a.json", "x")
        assert list(tmp_path.iterdir()) == [tmp_path / "a.json"]

    def test_failed_replace_keeps_original_and_cleans_temp(self, tmp_path):
        target = tmp_path / "a.json"
        target.write_text("original")
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with pytest.raises(OSError):
                atomic_write_text(target, "new-content")
        assert target.read_text() == "original"
        assert list(tmp_path.iterdir()) == [target]
        assert not list(tmp_path.glob(".*.tmp"))

    def test_creates_missing_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "a.json"
        atomic_write_text(target, "x")
        assert target.read_text() == "x"

    def test_rejected_by_os_umask(self, tmp_path):
        # Unwritable directory surfaces the OSError (no silent swallow).
        target = tmp_path / "a.json"
        with mock.patch("os.fdopen", side_effect=OSError("denied")):
            with pytest.raises(OSError):
                atomic_write_text(target, "x")
        assert not target.exists()
        assert not list(tmp_path.glob(".*.tmp"))

    def test_default_write_fsyncs_file_and_parent(self, tmp_path, monkeypatch):
        """The default is durable: the temp file and the parent rename both fsync."""
        calls: list[int] = []
        _watch_fsync(monkeypatch, calls)
        atomic_write_text(tmp_path / "a.json", "x")
        assert len(calls) == 2, f"file + parent must fsync, got {len(calls)}"

    def test_best_effort_write_skips_fsync(self, tmp_path, monkeypatch):
        """``durable=False`` writes the replace but pays no fsync: a best-effort
        store loses the entry whole to power loss, never half-written (#357)."""
        calls: list[int] = []
        _watch_fsync(monkeypatch, calls)
        atomic_write_text(tmp_path / "a.json", "x", durable=False)
        assert (tmp_path / "a.json").read_text() == "x"
        assert calls == [], f"a best-effort write must not fsync, got {len(calls)}"
