"""A comment block longer than two lines is documentation wearing a comment.

Inline ``#`` comments exist to carry the line-local WHY. A third consecutive
line means the block stopped annotating code and started substituting for a
docstring, a commit message, or a design note -- and every one of those has a
better home. The limit is two consecutive full-line comments; a blank line or
any code line ends a run.

The tree carried 1013 such runs (431 in app/, 582 in tests/) when this landed,
so the property starts as a budget ratchet in the pyright-budget shape: the
declared count is exact, a new long run fails the commit, and a wave that
trims runs must lower the number in the same commit. The only direction the
budget moves is down.
"""

from __future__ import annotations

from tests.chokepoint_walks import REPO_ROOT

#: The most consecutive full-line ``#`` comments a block may run.
LIMIT = 2

#: The measured long-run count per root, at the last wave. Exact equality both
#: ways: a run fails, a trimmed tree demands the lower number in this commit.
_BUDGETS = {
    "app": 409,
    "tests": 582,
}


def long_comment_runs(source: str) -> list[tuple[int, int]]:
    """Every maximal run of consecutive full-line comments longer than LIMIT.

    Returns ``(start, end)`` line numbers (1-based, inclusive) for runs of
    strictly more than ``LIMIT`` lines; a run is broken by any line that is
    not a full-line comment, including blank lines.
    """
    runs: list[tuple[int, int]] = []
    start = 0
    for lineno, line in enumerate(source.splitlines(), start=1):
        if line.strip().startswith("#"):
            if not start:
                start = lineno
            continue
        if start and lineno - start > LIMIT:
            runs.append((start, lineno - 1))
        start = 0
    if start and len(source.splitlines()) - start + 1 > LIMIT:
        runs.append((start, len(source.splitlines())))
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
    assert long_comment_runs("# one\n\n# two\n# three\n") == []


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
