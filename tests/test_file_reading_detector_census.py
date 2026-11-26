"""Class C: file-reading detector invariants are admitted by name, never faked as Rules."""

from __future__ import annotations

import pathlib

from tests.rule_census import (
    FILE_READING_DETECTOR_CEILING,
    FILE_READING_DETECTOR_SET,
    as_relative,
    ast_scanning_modules,
    file_reading_detector_modules,
    rule_only_modules,
)

_RULE_ONLY = "from tests.rules import Rule\nRULE = Rule('x', lambda: [], lambda: 'x = 1')\n"


class TestFileReadingDetectorAdmission:
    """A file-reading detector is a named, down-only exception to class-A zero."""

    def test_no_new_file_reading_detector_module(self):
        found = set(as_relative(rule_only_modules()))
        assert not (new := found - FILE_READING_DETECTOR_SET), (
            "new file-reading detector invariant(s) must be admitted by name in class C: "
            + ", ".join(sorted(new))
        )

    def test_no_stale_file_reading_detector_path(self):
        found = set(as_relative(rule_only_modules()))
        assert not (stale := FILE_READING_DETECTOR_SET - found), (
            "admitted class-C path(s) now construct a Registry or vanished; remove them from the named set: "
            + ", ".join(sorted(stale))
        )

    def test_the_file_reading_detector_ceiling_is_not_stale(self):
        found = rule_only_modules()
        assert len(found) == FILE_READING_DETECTOR_CEILING, (
            f"file-reading detector ceiling {FILE_READING_DETECTOR_CEILING} is stale; "
            f"tree has {len(found)} module(s): " + ", ".join(as_relative(found))
        )

    def test_class_c_admission_is_path_exact(self, tmp_path: pathlib.Path):
        """A same-basename decoy outside the named path stays in class A, never absorbed by C."""
        (tmp_path / "test_doc_contracts.py").write_text(_RULE_ONLY, encoding="utf-8")
        assert file_reading_detector_modules(tmp_path) == []
        assert [path.name for path in ast_scanning_modules(tmp_path)] == ["test_doc_contracts.py"]
