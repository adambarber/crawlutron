"""A comment block longer than two lines is documentation wearing a comment.

Inline ``#`` comments exist to carry the line-local WHY. A third comment line
in one block means it stopped annotating code and started substituting for a
docstring, a commit message, or a design note -- and every one of those has a
better home. The limit is two comment lines per block: a code line ends a
block, a blank line DOES NOT -- a block split by blank lines is still one
block, and the split is a cheat this gate exists to catch.

The tree measured 954 such blocks (390 in app/, 564 in tests/) under the
hardened blank-joined rule, so the property starts as a budget ratchet in the pyright-budget shape: the
declared count is exact, a new long block fails the commit, and a wave that
trims blocks must lower the number in the same commit. The only direction the
budget moves is down.
"""

from __future__ import annotations

from tests.chokepoint_walks import REPO_ROOT

#: The most consecutive full-line ``#`` comments a block may run.
LIMIT = 2

#: The measured long-run count per root, at the last wave. Exact equality both
#: ways: a run fails, a trimmed tree demands the lower number in this commit.
_BUDGETS = {
    "app": 387,
    "tests": 564,
}


def long_comment_runs(source: str) -> list[tuple[int, int]]:
    """Every blank-joined comment block of more than ``LIMIT`` comment lines.

    A block is a maximal run of comment lines joined across blank lines and
    broken only by a code line; its length is its comment-line count. Returns
    ``(start, end)`` (1-based, first and last comment line) for blocks longer
    than ``LIMIT``.
    """
    runs: list[tuple[int, int]] = []
    count = 0
    first = last = 0
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            count += 1
            if count == 1:
                first = lineno
            last = lineno
        elif not stripped:
            continue  # a blank line does not end a comment block
        else:
            if count > LIMIT:
                runs.append((first, last))
            count = 0
    if count > LIMIT:
        runs.append((first, last))
    return runs


def _root_runs(root: str) -> dict[str, list[tuple[int, int]]]:
    """Every long run under one root, as ``{file: [(start, end), ...]}``."""
    found: dict[str, list[tuple[int, int]]] = {}
    for path in sorted((REPO_ROOT / root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        runs = long_comment_runs(path.read_text(encoding="utf-8"))
        if runs:
            found[path.relative_to(REPO_ROOT).as_posix()] = runs
    return found


def test_the_detector_bites_on_a_third_consecutive_comment() -> None:
    """A three-line block fires; the legal shapes stay clean."""
    assert long_comment_runs("# one\n# two\n# three\n") == [(1, 3)]
    assert long_comment_runs("# one\n# two\nx = 1\n") == []
    assert long_comment_runs("# one\n\n# two\n") == []


def test_blank_lines_do_not_split_a_block() -> None:
    """A block chopped by blank lines is still one block -- the split is a
    cheat this gate exists to catch, not a formatting choice it blesses."""
    assert long_comment_runs("# one\n\n# two\n# three\n") == [(1, 4)]
    assert long_comment_runs("# one\n# two\n\n# three\n# four\n\n# five\n") == [(1, 7)]


def test_every_root_is_at_its_declared_comment_budget() -> None:
    """No new long comment run lands, and no trim is left unclaimed."""
    failures: list[str] = []
    for root, budget in _BUDGETS.items():
        found = _root_runs(root)
        total = sum(len(runs) for runs in found.values())
        if total != budget:
            worst = sorted(found.items(), key=lambda kv: -len(kv[1]))[:5]
            detail = "; ".join(f"{name}: {len(runs)} runs" for name, runs in worst)
            failures.append(
                f"{root}: {total} long comment runs, budget declares {budget} ({detail}). "
                "A new run must be cut to two lines or moved to a docstring; a trimmed "
                "tree must lower the budget in the same commit."
            )
    assert not failures, "\n".join(failures)
