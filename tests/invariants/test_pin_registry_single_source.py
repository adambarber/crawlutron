"""Version pins live only in the registry; the tree carries no SHA literals."""

from __future__ import annotations

import ast
import pathlib
import re
import tokenize

from tests._walk_primitives import scan_findings
from tests.chokepoint_paths import REPO_ROOT

_BARE_SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_SHA = "0123456789" * 4

# Fixtures below hold hypothetical pins for OTHER gates; their SHAs are data, not pins.
_FIXTURE_SHA_PATHS = frozenset(
    {
        "tests/release_bites/test_missing_member_dep.py",
        "tests/workflows/bites/test_silent_uploads.py",
    }
)


def _string_value(token_text: str) -> str:
    """The literal's value without quotes; f-strings fall back to their token text."""
    try:
        value = ast.literal_eval(token_text)
    except (ValueError, SyntaxError):
        return token_text
    return value if isinstance(value, str) else token_text


def _sha_literals(path: pathlib.Path, relative: str | None = None) -> list[tuple[int, str]]:
    """String tokens carrying a bare commit SHA, as (line, value)."""
    rel = relative or (
        path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.name
    )
    if rel in _FIXTURE_SHA_PATHS:
        return []
    with path.open("rb") as stream:
        try:
            tokens = list(tokenize.tokenize(stream.readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return []
    found: list[tuple[int, str]] = []
    for token in tokens:
        if token.type == tokenize.STRING:
            value = _string_value(token.string)
            if _BARE_SHA.search(value):
                found.append((token.start[0], value))
    return found


def test_no_commit_sha_literal_outside_registry() -> None:
    """One loop reports every file:line pin literal, so no failure hides another."""
    failures = scan_findings(_sha_literals)
    assert not failures, "pin literal outside .github/versions.toml:\n" + "\n".join(failures)


def test_a_bare_sha_literal_outside_the_registry_is_detected(tmp_path: pathlib.Path) -> None:
    """Bite: a planted 40-hex pin must be reported, or the gate is vacuous."""
    planted = tmp_path / "planted.py"
    planted.write_text(f'PIN = "{_SHA}"\n', encoding="utf-8")
    assert _sha_literals(planted) == [(1, _SHA)]


def test_longer_hex_runs_are_not_misread_as_commit_shas(tmp_path: pathlib.Path) -> None:
    """A 64-char SHA256 or caller key holds a 40-hex substring but is no pin."""
    sha256 = _SHA + _SHA[:24]
    key = "ck_" + "0" * 64
    planted = tmp_path / "digests.py"
    planted.write_text(f'SHA256 = "{sha256}"\nKEY = "{key}"\n', encoding="utf-8")
    assert not _sha_literals(planted)


def test_fixture_sha_paths_admit_only_their_exact_paths(tmp_path: pathlib.Path) -> None:
    """Fixture SHAs are allowed only at their exact path; a basename decoy still REDs."""
    source = f'PIN = "{_SHA}"\n'

    def plant(relative: str) -> pathlib.Path:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        return target

    for relative in _FIXTURE_SHA_PATHS:
        assert not _sha_literals(plant(relative), relative=relative)
        decoy = "decoy/" + relative
        assert _sha_literals(plant(decoy), relative=decoy) == [(1, _SHA)]
