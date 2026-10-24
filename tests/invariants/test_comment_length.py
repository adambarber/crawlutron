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


# Extensionless-ish entry points the project actually runs, swept with the .py
# files so a long run in them IS caught.
_ENTRY_POINTS = ("scripts/gate", "scripts/run-tests.sh")


def _root_findings[T](
    root: str, collect: Callable[[str], list[T]], *, include_gate_bash: bool = False
) -> dict[str, list[T]]:
    """Every ``collect`` hit under one root, as ``{file: [findings]}``.

    ``include_gate_bash`` adds the entry-point scripts (scripts/gate and
    scripts/run-tests.sh) to the sweep; the docstring scan never asks for them.
    """
    files = sorted((REPO_ROOT / root).rglob("*.py"))
    if include_gate_bash and root == "scripts":
        for name in _ENTRY_POINTS:
            path = REPO_ROOT / name
            if path.exists():
                files.append(path)
    found: dict[str, list[T]] = {}
    for path in files:
        if "__pycache__" in path.parts:
            continue
        findings = collect(path.read_text(encoding="utf-8"))
        if findings:
            found[path.relative_to(REPO_ROOT).as_posix()] = findings
    return found


def _root_runs(root: str) -> dict[str, list[tuple[int, int]]]:
    """Every long run under one root, as ``{file: [(start, end), ...]}``."""
    return _root_findings(root, long_comment_runs, include_gate_bash=True)


def _root_docstrings(root: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every over-limit docstring under one root, as ``{file: [(start, end, kind)]}``.

    The extractor owns the walk; this gate parses once per file, applies the
    length decision, and checks the root's budget.
    """

    def collect(source: str) -> list[tuple[int, int, str]]:
        tree = ast.parse(source)
        return over_line_docstrings(tree, DOC_LIMIT)

    return _root_findings(root, collect)


def demoted_docstrings(source: str) -> list[tuple[int, int, str]]:
    """Every docstring that sits below the first statement, so it documents nothing.

    A string expression is a docstring only when it is the first statement;
    below an annotation it is a dead string the object does not carry.
    """
    tree = ast.parse(source)
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_docstring(node) is not None:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Expr):
                continue
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                found.append((statement.lineno, statement.end_lineno or statement.lineno, node.name))
                break
    return found


def _root_all_blocks(root: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every dangling block under one root, as ``{file: [(start, end, text)]}``."""
    return _root_findings(root, dangling_comment_blocks)


def test_scripts_is_part_of_the_comment_budget_surface() -> None:
    """The gate sweeps scripts/, including the extensionless gate helper.

    The banner used to survive because scripts/ was outside the comment-budget
    surface and a bash script has no ``.py`` suffix to be picked up by the glob.
    """
    assert _root_runs("scripts") == {}
    # A three-line block in the helper surface fires; the banner shape is gone.
    assert long_comment_runs("#!/bin/sh\n# one\n# two\n# three\n") == [(1, 4)]


def test_the_run_tests_entry_point_is_inside_the_surface() -> None:
    # scripts/run-tests.sh is swept: a planted long run in it FAILS the budget.
    # The file was invisible after its move into scripts/.

    surface_files = sorted((REPO_ROOT / "scripts").rglob("*.py")) + [
        p for name in _ENTRY_POINTS if (p := REPO_ROOT / name).exists()
    ]
    assert REPO_ROOT / "scripts" / "run-tests.sh" in surface_files
    source = (REPO_ROOT / "scripts" / "run-tests.sh").read_text(encoding="utf-8")
    assert long_comment_runs(source) == [], "a run here is trimmed, never budgeted"
    planted = source + "\n# one\n# two\n# three\n"
    assert long_comment_runs(planted), "the detector must fire on a planted run"


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


def _zero_everywhere[T](collect: Callable[[str], list[T]]) -> list[tuple[str, T]]:
    """Every finding of ``collect`` across all three roots, with file and raw item."""
    offenders: list[tuple[str, T]] = []
    for root in ("app", "tests", "scripts"):
        for name, items in _root_findings(root, collect).items():
            for item in items:
                offenders.append((name, item))
    return offenders


def _format_long_comment_run(name: str, item: tuple[int, int]) -> str:
    return f"{name}:{item[0]}: {item[-1]}"


def _format_overlong_docstring(name: str, item: tuple[int, int, str]) -> str:
    lineno, _end, kind = item
    return f"{name}:{lineno}: {kind} exceeds three lines"


def test_zero_long_comment_runs_everywhere() -> None:
    """Zero long comment runs in app/, tests/ and scripts/ -- no budget, no exemption."""
    offenders = [_format_long_comment_run(name, item) for name, item in _zero_everywhere(long_comment_runs)]
    assert not offenders, "\n".join(offenders)


def test_no_overlong_docstring_anywhere() -> None:
    """Zero over-limit docstrings in app/, tests/ and scripts/ -- no budget, no exemption."""

    def collect(source: str) -> list[tuple[int, int, str]]:
        return over_line_docstrings(ast.parse(source), DOC_LIMIT)

    offenders = [_format_overlong_docstring(name, item) for name, item in _zero_everywhere(collect)]
    assert not offenders, "\n".join(offenders)


def test_no_docstring_sits_below_the_first_statement() -> None:
    """A docstring below the first statement documents nothing.

    The length gate could not see this: the string was a statement, so it had
    a length, and the object it claimed to document carried no documentation.
    """
    failures: list[str] = []
    for root in ("app", "tests", "scripts"):
        for name, items in _root_findings(root, demoted_docstrings).items():
            for lineno, _end, symbol in items:
                failures.append(f"{name}:{lineno}: {symbol} has a docstring that documents nothing")
    assert not failures, "\n".join(failures)


def test_no_comment_block_ends_mid_clause() -> None:
    """Every block in the tree closes the thought it opens."""
    offenders: list[str] = []
    for root in ("app", "tests", "scripts"):
        for name, found in _root_all_blocks(root).items():
            for start, _end, text in found:
                offenders.append(f"{name}:{start}: {text}")
    assert not offenders, "\n".join(offenders)
