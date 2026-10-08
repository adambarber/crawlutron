"""atomic_write chokepoint contract: every disk write is crash-safe.

The cookie jar hand-rolled mkstemp + fsync + os.replace; ContentCache used
bare path.write_text (a mid-write crash leaves a corrupt file survived only
because the reader swallows ValueError). One helper, one audited dance.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.safety.atomic_write import atomic_write_text


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
