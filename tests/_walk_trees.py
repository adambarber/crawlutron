"""File-tree walks with exemptions and offender sweeps."""

import ast
import pathlib
import tokenize
from collections.abc import Callable, Iterable, Iterator
from functools import cache

from tests._walk_primitives import APP_ROOTS, REPO_ROOT, _narrow, parsed, trees_under

# crawlutron/safety/safe_regex.py is the sanctioned regex wrapper (the third-party `regex`
# engine); only the regex-engine-import rule excludes it, never a walk default.
SANCTIONED = frozenset({"safe_regex.py"})


def trees_excluding(root: pathlib.Path, *sanctioned: str) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """``trees_under`` without the named files.

    Exemptions used to be a ``continue`` per detector, so a forgotten one
    flagged the file the rule carves out; the exemption belongs to the walk."""
    skip = frozenset(sanctioned)
    return ((path, tree) for path, tree in trees_under(root) if path.name not in skip)


def functions(root: ast.AST) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    """Every function definition in `root`, and nothing else.

    Replaces ``_functions_with``, which also yielded ``ast.Module`` -- paid for
    in seven detectors each opening with an ``isinstance`` guard to drop it."""
    return tuple(
        node
        for node in _narrow(root, "function", ast.FunctionDef, ast.AsyncFunctionDef)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def offenders_over_functions(
    root: ast.AST,
    check: Callable[[ast.FunctionDef | ast.AsyncFunctionDef], Iterable[str]],
) -> list[str]:
    """What `check` flags across every function in `root`, concatenated.

    Spelled per detector, a copy settles "every function" differently; the
    walk belongs to one place and the per-function rule is all it should say."""
    return [offender for node in functions(root) for offender in check(node)]


_STRUCTURAL_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


def app_python_files(*, exclude: frozenset[str] = frozenset()) -> tuple[pathlib.Path, ...]:
    """Every app/*.py module except the exclude set's names.

    The default sees ALL modules -- the sanctioned exclude belongs to the rule
    that owns it, not the walk (a broad default hid safe_regex from every detector)."""
    return _app_files(frozenset(exclude))


def app_trees(*, exclude: frozenset[str] = frozenset()) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """``(path, tree)`` for the same files ``app_python_files`` walks.

    A reusable tuple rather than a generator, because generators defer
    traversal -- each caller saw its own window onto a moving tree."""
    return _app_trees(frozenset(exclude))


@cache
def _app_files(exclude: frozenset[str]) -> tuple[pathlib.Path, ...]:
    """The one glob of app/*.py across every production package root, memoized per exclude set."""
    return tuple(sorted(p for root in APP_ROOTS for p in root.rglob("*.py") if p.name not in exclude))


@cache
def _app_trees(exclude: frozenset[str]) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """``(path, tree)`` per exclude set, sharing each file's one cached parse.

    The exclude-set key is per-list only; the parse lives in ``parsed``, keyed
    on the file itself, so fifteen exclude lists still parse each file once.
    """
    return tuple((path, parsed(path)) for path in _app_files(exclude))


def relative_posix(path: pathlib.Path) -> str:
    """``crawlutron/admission/admission.py`` -- the label a detector exempts a path by."""
    return path.relative_to(REPO_ROOT).as_posix()


def app_offenders(
    detector,
    *,
    exclude: frozenset[str] = frozenset(),
    skip: tuple[str, ...] = (),
    kwargs_for: Callable[[str], dict[str, object]] | None = None,
    **detector_kwargs,
) -> list[str]:
    """Run one app-tree detector over every module and collect its offenders.

    ``skip`` holds the repo-relative paths the detector's contract exempts;
    ``kwargs_for`` supplies per-module kwargs instead of per-test loops."""
    exempt = {str(item) for item in skip}
    offenders: list[str] = []
    for path, tree in app_trees(exclude=exclude):
        relative = relative_posix(path)
        if relative in exempt:
            continue
        per_path = kwargs_for(relative) if kwargs_for is not None else {}
        offenders.extend(detector(tree, path, **detector_kwargs, **per_path))
    return offenders
