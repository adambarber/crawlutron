"""A comment block longer than two lines is documentation wearing a comment.

Inline ``#`` comments exist to carry the line-local WHY. A third comment line
in one block means it stopped annotating code and started substituting for a
docstring, a commit message, or a design note -- and every one of those has a
better home. The limit is two comment lines per block: a code line ends a
block, a blank line DOES NOT -- a block split by blank lines is still one
block, and the split is a cheat this gate exists to catch.

A second property rides the same walk: a block's last line must CLOSE its
thought. Trimming a block at the length limit is a rewrite, and a rewrite can
end mid-clause -- ``the cap is a fleet ceiling, and`` says less than the block
it replaced while passing the length rule. The dangling-end detector names that
shape wherever it lands, which is what lets the trims stay mechanical.

The tree measured 954 such blocks (390 in app/, 564 in tests/) under the
hardened blank-joined rule, so the property starts as a budget ratchet in the pyright-budget shape: the
declared count is exact, a new long block fails the commit, and a wave that
trims blocks must lower the number in the same commit. The only direction the
budget moves is down.
"""

from __future__ import annotations

import re

from tests.chokepoint_walks import REPO_ROOT

#: The most consecutive full-line ``#`` comments a block may run.
LIMIT = 2

#: The measured long-run count per root, at the last wave. Exact equality both
#: ways: a run fails, a trimmed tree demands the lower number in this commit.
_BUDGETS = {
    "app": 183,
    "tests": 564,
}

#: A block's last line may not stop mid-clause: connectives, trailing commas and
#: dangling dashes are a cut clause. A trailing run of 3+ dashes is a banner, kept.
_DANGLING_END = re.compile(
    r"(,|(?<!-)--|[ ](and|or|but|so|which|that|the|a|an|of|to|with|without|"
    r"not|as|by|for|from|in|into|on|at|per|than|then|when|while|because|"
    r"unless|until|where|whether|is|are|was|were|be|been|if|it|its|their|"
    r"our|your|no|each|every|either|neither|both|all|some|any|most|more|"
    r"less|other|another|such|via|using))$"
)

#: A line whose whole tail is a dash run is a divider or banner, never a clause.
_DECORATIVE_TAIL = re.compile(r"-{3,}\s*$")


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


def _root_all_blocks(root: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every dangling block under one root, as ``{file: [(start, end, text)]}``."""
    found: dict[str, list[tuple[int, int, str]]] = {}
    for path in sorted((REPO_ROOT / root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        blocks = dangling_comment_blocks(path.read_text(encoding="utf-8"))
        if blocks:
            found[path.relative_to(REPO_ROOT).as_posix()] = blocks
    return found


def _comment_blocks(source: str) -> list[tuple[int, int, str]]:
    """Every blank-joined comment block, as ``(start, end, last_text)``."""
    blocks: list[tuple[int, int, str]] = []
    count = 0
    first = last = 0
    last_text = ""
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            count += 1
            if count == 1:
                first = lineno
            last = lineno
            last_text = stripped
        elif not stripped:
            continue
        else:
            if count:
                blocks.append((first, last, last_text))
            count = 0
    if count:
        blocks.append((first, last, last_text))
    return blocks


def dangling_comment_blocks(source: str) -> list[tuple[int, int, str]]:
    """Blocks whose last line stops mid-clause, with the offending line."""
    return [
        (start, end, text)
        for start, end, text in _comment_blocks(source)
        if _DANGLING_END.search(text) and not _DECORATIVE_TAIL.search(text)
    ]


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


def test_a_block_cut_mid_clause_fires() -> None:
    """The dangling-end detector bites on a cut clause, and only on that."""
    assert dangling_comment_blocks("# the cap is a fleet ceiling, and\nx = 1\n") == [
        (1, 1, "# the cap is a fleet ceiling, and")
    ]
    assert dangling_comment_blocks("# so the budget is\n# charged in the\nx = 1\n") == [
        (1, 2, "# charged in the")
    ]
    assert dangling_comment_blocks("# The cap is a fleet ceiling.\nx = 1\n") == []
    assert dangling_comment_blocks("# done: a table constraint follows\nx = 1\n") == []
    assert dangling_comment_blocks("# --- Structured discovery ---\nx = 1\n") == []


def test_no_comment_block_ends_mid_clause() -> None:
    """Every block in the tree closes the thought it opens."""
    offenders: list[str] = []
    for root in _BUDGETS:
        for name, found in _root_all_blocks(root).items():
            for start, _end, text in found:
                offenders.append(f"{name}:{start}: {text}")
    assert not offenders, "\n".join(offenders)
