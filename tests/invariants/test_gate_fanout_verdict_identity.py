"""Verdict identity is content-bound: staged tree + config, never ref env vars."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from scripts.gate_fanout import _dispatch, _run_key


def _make_ls_files_mock(entries: list[str]):
    """Return a subprocess.run stub whose git ls-files --stage yields `entries`."""

    def fake_run(cmd, **kwargs):
        import subprocess

        if list(cmd)[1:3] == ["ls-files", "--stage"]:
            text = "\n".join(entries)
            if text:
                text += "\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def _run_key_with_entries(entries: list[str], index_file: str | None = None, tmp_path=None) -> str:
    """Call _run_key with git ls-files stubbed to return `entries`.

    No path is touched: Path.is_file is mocked so the staged branch is taken
    when an index path is supplied, and the adhoc branch when it is not.
    """
    if index_file is None and tmp_path is not None:
        index_file = str(tmp_path / "index")
    with patch("scripts.gate_fanout.subprocess.run", side_effect=_make_ls_files_mock(entries)):
        with patch.object(Path, "is_file", return_value=bool(index_file)):
            return _run_key(index_file=index_file)


def test_run_key_is_content_bound_not_ref_bound(tmp_path) -> None:
    """A changed staged tree yields a different key: no stale .rc reuse."""
    key_a = _run_key_with_entries(["100644 aaa 0\ta.py"], tmp_path=tmp_path)
    key_b = _run_key_with_entries(["100644 bbb 0\ta.py"], tmp_path=tmp_path)
    assert key_a != key_b


def test_run_key_is_deterministic_for_same_staged_tree(tmp_path) -> None:
    """Same staged tree + config yields the same key: legitimate reuse within a commit."""
    entries = ["100644 aaa 0\ta.py"]
    assert _run_key_with_entries(entries, tmp_path=tmp_path) == _run_key_with_entries(
        entries, tmp_path=tmp_path
    )


def test_adhoc_key_is_content_bound_and_stable(monkeypatch) -> None:
    """--all-files keys are stable for one worktree and change when it changes."""
    monkeypatch.delenv("PRE_COMMIT_INDEX_FILE", raising=False)
    monkeypatch.setattr("scripts.gate_fanout._worktree_digest", lambda: "tree-a")
    before = _run_key()
    assert before == _run_key()
    monkeypatch.setattr("scripts.gate_fanout._worktree_digest", lambda: "tree-b")
    assert _run_key() != before


def test_untracked_content_edit_changes_the_key(tmp_path, monkeypatch) -> None:
    """An untracked file's content is part of the no-index verdict identity."""
    monkeypatch.delenv("PRE_COMMIT_INDEX_FILE", raising=False)
    target = tmp_path / "untracked.py"
    target.write_text("pass\n")

    def fake_capture(args, env=None):
        if args[:3] == ["ls-files", "--others", "--exclude-standard"]:
            return f"{target}\0"
        return ""

    monkeypatch.setattr("scripts.gate_fanout._git_capture", fake_capture)
    before = _run_key()
    target.write_text("def broken(:\n")
    assert _run_key() != before


def test_run_key_differs_when_config_changes(tmp_path) -> None:
    """A config digest change yields a different key even with identical staged tree."""
    entries = ["100644 aaa 0\ta.py"]
    index_file = str(tmp_path / "index")
    original = b"# original config\n"
    changed = b"# changed config\n"
    orig_mock = Mock(spec=Path)
    orig_mock.read_bytes.return_value = original
    changed_mock = Mock(spec=Path)
    changed_mock.read_bytes.return_value = changed
    with patch("scripts.gate_fanout.CONFIG_PATH", orig_mock):
        key_before = _run_key_with_entries(entries, index_file=index_file)
    with patch("scripts.gate_fanout.CONFIG_PATH", changed_mock):
        key_after = _run_key_with_entries(entries, index_file=index_file)
    assert key_before != key_after


def test_dispatch_spawns_when_no_rc_exists(tmp_path, monkeypatch) -> None:
    """_dispatch spawns a gate child when no .rc exists for this key's dir."""
    spawned: list[str] = []

    def fake_spawn(hook_id, argv, status_dir):
        spawned.append(hook_id)

    with patch("scripts.gate_fanout._spawn", fake_spawn):
        status_root = tmp_path / "status"
        monkeypatch.setattr("scripts.gate_fanout.STATUS_ROOT", status_root)
        _dispatch(status_root / "fresh-key")
        assert "ruff" in spawned
