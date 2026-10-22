"""A file the tokenizer rejects is reported and skipped, never a crash."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.extract_comments import _records, main

BAD_DEDENT = "def f():\n    x = 1\n  y = 2\n"


def test_the_tokenizer_path_raises_syntaxerror_so_the_caller_names_the_file(
    tmp_path: Path,
) -> None:
    """tokenize.generate_tokens raises IndentationError (a SyntaxError subclass,
    never a TokenError) on a bad dedent. The tokenizer loop propagates it: the
    guard lives where the caller can name the skipped file.
    """
    broken = tmp_path / "broken.py"
    broken.write_text(BAD_DEDENT, encoding="utf-8")
    with pytest.raises(SyntaxError):
        list(_records(broken, include_docstrings=True))


def test_main_skips_and_names_an_unparseable_file(tmp_path: Path, capsys, monkeypatch) -> None:
    """The tool-level behaviour is EXPLICIT: the file is named in the report and
    the run completes -- not silently ignored, not a crash.
    """
    broken = tmp_path / "broken.py"
    broken.write_text(BAD_DEDENT, encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("# a comment\nx = 1\n", encoding="utf-8")
    out = tmp_path / "out.txt"
    monkeypatch.setattr("scripts.extract_comments.REPO_ROOT", tmp_path)
    assert main(["-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "skipping" in err and "broken.py" in err, f"the skipped file must be named:\n{err}"
    assert "unindent" in err, f"the cause must be reported:\n{err}"
    assert out.read_text().startswith("good.py:1:"), out.read_text()


def test_a_well_formed_file_still_yields_comments(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("# a comment\nx = 1\n", encoding="utf-8")
    assert list(_records(good, include_docstrings=True)) == [(1, 0, "# a comment")]
