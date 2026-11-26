"""A file the tokenizer rejects is reported and skipped, never a crash."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.extract_comments import _records, _touched_files, main
from tests.chokepoint_walks import discovered_files


def _text_of(path):
    """Read a test artifact through the sanctioned discovery door."""
    return discovered_files(path.parent, path.name, include_content=True)[path]


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
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
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


def _install_tree(monkeypatch, tmp_path):
    first = tmp_path / "first.py"
    first.write_text('"""One docstring."""\n# one comment\nx = 1\n', encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text("y = 2  # two comment\n", encoding="utf-8")
    monkeypatch.setattr("scripts.extract_comments.REPO_ROOT", tmp_path)
    return first, second


def test_touched_emits_each_heading_and_every_record(tmp_path: Path, monkeypatch, capsys) -> None:
    first, second = _install_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "scripts.extract_comments._touched_files",
        lambda rev: [first, second],
    )
    out = tmp_path / "out.txt"
    assert main(["--touched", "BASE", "-o", str(out)]) == 0
    text = _text_of(out)
    lines = text.splitlines()
    assert lines[0] == "ENTIRETY OF FILE: first.py"
    assert "ENTIRETY OF FILE: second.py" in lines
    assert 'first.py:1:"""One docstring."""' in lines
    assert "first.py:2:# one comment" in lines
    assert "second.py:1:# two comment" in lines
    assert text.index("ENTIRETY OF FILE: first.py") < text.index("ENTIRETY OF FILE: second.py")
    assert "skipping" not in capsys.readouterr().err


def test_touched_derives_files_from_the_rev_range(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("scripts.extract_comments.REPO_ROOT", tmp_path)
    monkeypatch.setattr("scripts.extract_comments.shutil.which", lambda name: "/usr/bin/git")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(stdout=b"first.py\nsecond.py\n")

    monkeypatch.setattr("scripts.extract_comments.subprocess.run", fake_run)
    assert _touched_files("BASE") == [tmp_path / "first.py", tmp_path / "second.py"]
    assert seen[0][0] == ["/usr/bin/git", "diff", "--name-only", "BASE..HEAD"]


def test_touched_accepts_an_explicit_rev_range(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("scripts.extract_comments.REPO_ROOT", tmp_path)
    monkeypatch.setattr("scripts.extract_comments.shutil.which", lambda name: "/usr/bin/git")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(stdout=b"first.py\n")

    monkeypatch.setattr("scripts.extract_comments.subprocess.run", fake_run)
    assert _touched_files("BASE..OTHER") == [tmp_path / "first.py"]
    assert seen == [["/usr/bin/git", "diff", "--name-only", "BASE..OTHER"]]


def test_touched_passes_the_revision_to_the_derivation(tmp_path: Path, monkeypatch) -> None:
    first, _second = _install_tree(monkeypatch, tmp_path)
    seen = []

    def fake(rev):
        seen.append(rev)
        return [first]

    monkeypatch.setattr("scripts.extract_comments._touched_files", fake)
    out = tmp_path / "out.txt"
    assert main(["--touched", "BASE..HEAD", "-o", str(out)]) == 0
    assert seen == ["BASE..HEAD"]


def test_touched_emits_records_a_grep_would_filter(tmp_path: Path, monkeypatch) -> None:
    first, second = _install_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "scripts.extract_comments._touched_files",
        lambda rev: [first, second],
    )
    out = tmp_path / "out.txt"
    assert main(["--touched", "BASE", "--grep", "nomatch", "-o", str(out)]) == 0
    text = _text_of(out)
    assert "second.py:1:# two comment" in text


def test_untouched_extract_keeps_its_original_shape(tmp_path: Path, monkeypatch) -> None:
    first, _second = _install_tree(monkeypatch, tmp_path)
    monkeypatch.setattr("scripts.extract_comments._tracked_files", lambda: [first])
    out = tmp_path / "out.txt"
    assert main(["-o", str(out)]) == 0
    text = _text_of(out)
    assert "ENTIRETY OF FILE" not in text
    assert text.startswith("first.py:1:")
