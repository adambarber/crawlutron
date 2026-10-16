"""A comment block longer than two lines is documentation wearing a comment.

Inline comments carry line-local WHY; a third line substitutes for a docstring.
The count is a budget ratchet, fed by one scan, that only moves down.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

from scripts.extract_comments import over_line_docstrings
from tests.chokepoint_walks import REPO_ROOT

#: The most consecutive full-line ``#`` comments a block may run.
LIMIT = 2

#: Compile-time, runtime and test docstrings may run no longer than three lines.
DOC_LIMIT = 3

#: The measured over-length docstring count per root, at the last wave.
#: Exact equality both ways: new long docstrings fail; a trimmed tree lowers it.
_DOC_BUDGETS = {
    "app": 333,
    "tests": 0,
    "scripts": 33,
}

#: The measured long-run count per root, at the last wave. Exact equality both ways: a run fails, a trimmed
#: tree demands the lower number in this commit.
_BUDGETS = {
    "app": 0,
    "tests": 0,
}

#: A block's last line may not stop mid-clause: connectives, trailing commas and dangling dashes are a cut
#: clause. A trailing run of 3+ dashes is a banner, kept.
_DANGLING_END = re.compile(
    r"(,|(?<!-)--| (and|or|but|so|which|that|the|a|an|of|to|with|without|"
    r"not|as|by|for|from|in|into|on|at|per|than|then|when|while|because|"
    r"unless|until|where|whether|is|are|was|were|be|been|if|it|its|their|"
    r"our|your|no|each|every|either|neither|both|all|some|any|most|more|"
    r"less|other|another|such|via|using))$"
)

#: A line whose whole tail is a dash run is a divider or banner, never a clause.
_DECORATIVE_TAIL = re.compile(r"-{3,}\s*$")


def comment_blocks(source: str) -> list[tuple[int, int, int, str]]:
    """Every blank-joined comment block as ``(start, end, lines, last_text)``.

    One scan feeds both detectors; the two metrics differ only in which block
    row they test, so the line count and last line travel with the block.
    """
    blocks: list[tuple[int, int, int, str]] = []
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
            continue  # a blank line does not end a comment block
        else:
            if count:
                blocks.append((first, last, count, last_text))
            count = 0
    if count:
        blocks.append((first, last, count, last_text))
    return blocks


def long_comment_runs(source: str) -> list[tuple[int, int]]:
    """Every blank-joined comment block of more than ``LIMIT`` comment lines."""
    return [(start, end) for start, end, count, _ in comment_blocks(source) if count > LIMIT]


def dangling_comment_blocks(source: str) -> list[tuple[int, int, str]]:
    """Blocks whose last line stops mid-clause, with the offending line."""
    return [
        (start, end, text)
        for start, end, _count, text in comment_blocks(source)
        if _DANGLING_END.search(text) and not _DECORATIVE_TAIL.search(text)
    ]


def _root_findings[T](root: str, collect: Callable[[str], list[T]]) -> dict[str, list[T]]:
    """Every ``collect`` hit under one root, as ``{file: [findings]}``."""
    found: dict[str, list[T]] = {}
    for path in sorted((REPO_ROOT / root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        findings = collect(path.read_text(encoding="utf-8"))
        if findings:
            found[path.relative_to(REPO_ROOT).as_posix()] = findings
    return found


def _root_runs(root: str) -> dict[str, list[tuple[int, int]]]:
    """Every long run under one root, as ``{file: [(start, end), ...]}``."""
    return _root_findings(root, long_comment_runs)


def _root_docstrings(root: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every over-limit docstring under one root, as ``{file: [(start, end, kind)]}``.

    The extractor owns the walk; this gate parses once per file, applies the
    length decision, and checks the root's budget.
    """

    def collect(source: str) -> list[tuple[int, int, str]]:
        tree = ast.parse(source)
        return over_line_docstrings(tree, DOC_LIMIT)

    return _root_findings(root, collect)


def _root_all_blocks(root: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every dangling block under one root, as ``{file: [(start, end, text)]}``."""
    return _root_findings(root, dangling_comment_blocks)


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


def test_every_root_is_at_its_declared_docstring_budget() -> None:
    """No over-limit docstring lands, and no rewrite is left unclaimed."""
    failures: list[str] = []
    for root, budget in _DOC_BUDGETS.items():
        found = _root_docstrings(root)
        total = sum(len(items) for items in found.values())
        if total != budget:
            worst = sorted(found.items(), key=lambda kv: -len(kv[1]))[:3]
            detail = "; ".join(f"{name}: {len(items)}" for name, items in worst)
            failures.append(
                f"{root}: {total} docstrings over {DOC_LIMIT} lines, budget declares {budget} "
                f"({detail}). A new over-limit docstring fails; a rewritten tree must "
                "lower the budget in the same commit."
            )
    assert not failures, "\n".join(failures)


def test_no_comment_block_ends_mid_clause() -> None:
    """Every block in the tree closes the thought it opens."""
    offenders: list[str] = []
    for root in _BUDGETS:
        for name, found in _root_all_blocks(root).items():
            for start, _end, text in found:
                offenders.append(f"{name}:{start}: {text}")
    assert not offenders, "\n".join(offenders)
