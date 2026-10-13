"""Every Python file in app/, tests/ and scripts/ holds at most 500 lines.

The count is total lines: prose spends the same scroll as code. A file that
outgrows the ceiling splits, keeping each rationale beside the code it explains.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable, Iterator

from tests.chokepoint_walks import REPO_ROOT

#: Total lines one file may hold. 501 breaches; 500 is the maximum.
MAX_FILE_LINES = 500

#: All three trees, so neither the suite nor the gates grow past the ceiling.
ROOTS = ("app", "tests", "scripts")


def file_lines(text: str) -> int:
    """Total lines in ``text``: blank, comment, docstring and code all count."""
    return len(text.splitlines())


def python_files() -> Iterator[pathlib.Path]:
    """Every source file the ceiling governs, in a stable order."""
    for root in ROOTS:
        yield from sorted(
            path for path in (REPO_ROOT / root).rglob("*.py") if "__pycache__" not in path.parts
        )


def measured_files() -> list[tuple[str, int]]:
    """Every governed file as (repo-relative path, total lines)."""
    return [
        (path.relative_to(REPO_ROOT).as_posix(), file_lines(path.read_text(encoding="utf-8")))
        for path in python_files()
    ]


def offenders() -> list[tuple[str, int]]:
    """Files over the ceiling, worst first, as (path, total lines)."""
    return breaching(measured_files())


def breaching(measured: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    """The files in ``measured`` over the ceiling, worst first."""
    over = [(name, size) for name, size in measured if size > MAX_FILE_LINES]
    return sorted(over, key=lambda item: (-item[1], item[0]))


def excess(size: int) -> int:
    """How far past the ceiling a file of ``size`` lines reaches."""
    return size - MAX_FILE_LINES


def test_no_file_exceeds_the_line_ceiling() -> None:
    over = offenders()
    assert not over, (
        f"{len(over)} file(s) over the {MAX_FILE_LINES}-line ceiling; split the largest first, "
        "because this is a maximum and not a budget to re-raise: "
        + ", ".join(f"{name} ({size}, +{excess(size)})" for name, size in over)
    )


def test_the_ceiling_is_measured_over_all_three_trees() -> None:
    """An empty walk would read green; the roots must actually appear."""
    assert {name.split("/", 1)[0] for name, _ in measured_files()} == set(ROOTS)


def test_file_lines_counts_every_line() -> None:
    """Blank, comment, docstring and code lines all count."""
    text = 'def f():\n    """Why f exists."""\n\n    # how f works\n    return 1\n'
    assert file_lines(text) == 5


def test_a_file_past_the_ceiling_is_reported_with_its_excess() -> None:
    """One line over breaches, one line at the limit does not."""
    assert breaching([("synthetic.py", MAX_FILE_LINES + 1)]) == [("synthetic.py", MAX_FILE_LINES + 1)]
    assert breaching([("synthetic.py", MAX_FILE_LINES)]) == []
    assert excess(MAX_FILE_LINES + 1) == 1
