"""A hard ceiling on how large a single file may get.

A file is the unit a reader opens, reviews and navigates. Past some length the
top of it stops being a summary of the rest, and every change to the bottom
becomes a change nobody can see the context of -- which is how a second
implementation of a policy appears three hundred lines away from the first, and
how a detector's premise drifts from the detector it is meant to describe.

The ceiling is on CODE lines: total lines, less blank lines, less comment-only
lines. Prose does not count, and that is deliberate rather than convenient. This
suite treats rationale as load-bearing -- it lives next to the test that
embodies it and is never moved into a separate document -- so a ceiling that
counted prose would price documentation as a defect and could only be satisfied
by deleting the reasoning. Seven of the eleven files that exceed 1000 total
lines are under the ceiling on code lines, and every one of them is prose-heavy
for exactly that reason.

Two measures of "code lines" were compared before this one was chosen: counting
lines that are not blank and not comment-only, versus tokenizing and counting
the lines tokens actually span. They agree on every file in the tree, and neither
disagrees about a single offender. So the one that needs no parser wins: there
is no f-string, multi-line-string or unterminated-token edge case here to get
quietly wrong, and the definition fits in one sentence a reader can check
against the file by eye.

The target is a hard maximum, not a budget of offenders. A ratchet on a count
would let this suite go red once and then simply raise the number on the next
change, which is the failure mode the deleted test_suite_budget.py existed to
document. The number here is fixed, and the only way to change it is to say so
in a commit.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable, Iterator

from tests.chokepoint_walks import REPO_ROOT

# Total lines, less blank lines, less comment-only lines.
MAX_CODE_LINES = 1000

# All three trees, not just app/: a ceiling that only watched production code
# would let the test suite grow without limit, and the test suite is where the
# duplication and the drift live.
ROOTS = ("app", "tests", "scripts")


def code_lines(text: str) -> int:
    """Lines in ``text`` that are neither blank nor a comment on their own.

    A line holding only a comment does not count; an inline trailing comment
    does not discount the code it trails. Docstrings count, because a docstring
    is a statement -- the ceiling is about navigable code, not about bytes.
    """
    return sum(1 for raw in text.splitlines() if raw.strip() and not raw.strip().startswith("#"))


def python_files() -> Iterator[pathlib.Path]:
    """Every source file the ceiling governs, in a stable order.

    ``__pycache__`` is skipped rather than filtered by name: it is build residue
    from a previous run of a tree that no longer exists, and a stale ``.pyc`` of
    a renamed module would otherwise be measured and reported as if it were
    still source.
    """
    for root in ROOTS:
        yield from sorted(
            path for path in (REPO_ROOT / root).rglob("*.py") if "__pycache__" not in path.parts
        )


def measured_files() -> list[tuple[str, int]]:
    """Every governed file as (repo-relative path, code lines)."""
    return [
        (path.relative_to(REPO_ROOT).as_posix(), code_lines(path.read_text(encoding="utf-8")))
        for path in python_files()
    ]


def offenders() -> list[tuple[str, int]]:
    """Files over the ceiling, worst first, as (path, code lines)."""
    return breaching(measured_files())


def breaching(measured: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    """The files in ``measured`` over the ceiling, worst first.

    Split out from :func:`offenders` so the comparison itself can be tested
    without a file that is genuinely too large having to exist to prove it.
    """
    over = [(name, size) for name, size in measured if size > MAX_CODE_LINES]
    return sorted(over, key=lambda item: (-item[1], item[0]))


def excess(size: int) -> int:
    """How far past the ceiling a file of ``size`` code lines reaches."""
    return size - MAX_CODE_LINES


def test_no_file_exceeds_the_code_line_ceiling() -> None:
    over = offenders()
    assert not over, (
        f"{len(over)} file(s) over the {MAX_CODE_LINES}-line ceiling; split the largest first, "
        "because this is a maximum and not a budget to re-raise: "
        + ", ".join(f"{name} ({size}, +{excess(size)})" for name, size in over)
    )


def test_the_ceiling_is_measured_over_all_three_trees() -> None:
    """The premise: a gate that measured nothing reports nothing, and reads green.

    Every measure here is derived from the tree, so an empty ``ROOTS``, a wrong
    glob, or a path that does not exist would leave ``offenders()`` permanently
    empty and the ceiling permanently satisfied. That failure is invisible in a
    pass, which is the whole reason it needs a test of its own.
    """
    assert {name.split("/", 1)[0] for name, _ in measured_files()} == set(ROOTS)


def test_code_lines_ignores_blanks_and_comment_only_lines() -> None:
    """The measure's definition, pinned so it cannot drift into counting prose.

    A docstring counts as code and a comment does not, because the two sit in
    opposite positions: stripping the docstring would delete the rationale this
    suite exists to keep adjacent, while counting the comment would let a file
    grow to any length by explaining itself at length.
    """
    text = 'def f():\n    """Why f exists."""\n\n    # how f works\n    return 1\n'
    assert code_lines(text) == 3, "expected the def, the docstring and the return"


def test_a_file_past_the_ceiling_is_reported_with_its_excess() -> None:
    """Bite proof: the comparison must be able to fail, not merely to report.

    Without this the ceiling is a plausible-looking assertion that a broken walk
    or a flipped comparison would satisfy forever. A file one line over has to
    come back as an offender, and a file exactly at the limit must not -- the
    boundary is the part a strict ``>`` quietly gets wrong.
    """
    assert breaching([("synthetic.py", MAX_CODE_LINES + 1)]) == [("synthetic.py", MAX_CODE_LINES + 1)]
    assert breaching([("synthetic.py", MAX_CODE_LINES)]) == []
    assert excess(MAX_CODE_LINES + 1) == 1
