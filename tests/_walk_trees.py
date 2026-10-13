import ast
import pathlib
import tokenize
from collections.abc import Callable, Iterable, Iterator
from functools import cache

from tests._walk_primitives import APP_ROOT, REPO_ROOT, _narrow, trees_under

# app/safety/safe_regex.py is the sanctioned regex wrapper (stdlib `re` for literals; the third-party `regex`
# engine for bounded execution). It owns the banned primitives, so a full-tree walk excludes it.
SANCTIONED = frozenset({"safe_regex.py"})


def trees_excluding(root: pathlib.Path, *sanctioned: str) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """``trees_under`` without the named files.

    A check that exempts a file used to spell the exemption as a ``continue``
    inside its own loop, so the walk and the exemption were two decisions in
    every detector that had one -- and a detector that forgot the ``continue``
    flagged the very file the rule carves out, which reads as a broken rule
    rather than a missing exemption. The exemption belongs to the walk.

    The element is ``Module``, matching ``trees_under`` and ``app_trees``: this
    yields their trees unchanged, and only filters.
    """
    skip = frozenset(sanctioned)
    return ((path, tree) for path, tree in trees_under(root) if path.name not in skip)


def functions(root: ast.AST) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    """Every function definition in `root`, and nothing else.

    This replaces ``_functions_with``, which also yielded ``ast.Module``. That
    extra node was not a feature: a helper whose name says functions returned
    something that is not one, and the mismatch was paid for in seven detectors
    that each opened with an ``isinstance`` guard to drop it. Zero of them used
    it, so the lie only ever cost the guard.
    """
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

    The opening of most shape detectors: declare a list, walk the functions,
    append what each one offends, return. Spelled out per detector, a copy is
    free to settle "every function" differently -- :func:`functions` here, a raw
    :func:`ast.walk` with its own filter there -- and the two then disagree
    about the same file with no visible reason. Same argument as
    :func:`functions` narrowing its own return: the walk belongs to one place,
    and the per-function rule is the only thing a detector should be saying.
    """
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


def app_python_files(*, exclude: frozenset[str] = SANCTIONED) -> tuple[pathlib.Path, ...]:
    """Every app/*.py module except the sanctioned chokepoint wrappers.

    One glob per exclude set per session, returned as a tuple: the callers
    iterate it, and a shared sequence cannot be mutated into a second version
    of the tree. The cache is reached through the private function below rather
    than applied here, because a cache decorator keys on the CALL and not on the
    value -- `app_python_files()` and `app_python_files(exclude=SANCTIONED)`
    are two spellings of one argument, and memoizing the public signature
    would silently walk the tree twice.
    """
    return _app_files(frozenset(exclude))


def app_trees(*, exclude: frozenset[str] = SANCTIONED) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """``(path, tree)`` for the same files ``app_python_files`` walks.

    A reusable sequence rather than a generator, for the reason that matters
    here: a generator defers its traversal, so every caller's list-then-read
    became its own window onto a tree that may move. A tuple is one snapshot
    that a caller can hold, compare by identity, and re-read without touching
    the filesystem again.
    """
    return _app_trees(frozenset(exclude))


@cache
def _app_files(exclude: frozenset[str]) -> tuple[pathlib.Path, ...]:
    """The one glob of app/*.py, memoized per exclude set."""
    return tuple(sorted(p for p in APP_ROOT.rglob("*.py") if p.name not in exclude))


@cache
def _app_trees(exclude: frozenset[str]) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The one read-and-parse of app/*.py, memoized per exclude set.

    Built from ``_app_files`` under the same key, so a caller that wants only
    the paths and a caller that wants the trees share one glob.
    """
    return tuple((path, ast.parse(path.read_text(encoding="utf-8"))) for path in _app_files(exclude))


def relative_posix(path: pathlib.Path) -> str:
    """``app/admission/admission.py`` -- the label a detector exempts a path by."""
    return path.relative_to(REPO_ROOT).as_posix()


def app_offenders(
    detector,
    *,
    exclude: frozenset[str] = SANCTIONED,
    skip: tuple[str, ...] = (),
    kwargs_for: Callable[[str], dict[str, object]] | None = None,
    **detector_kwargs,
) -> list[str]:
    """Run one app-tree detector over every module and collect its offenders.

    ``skip`` holds repo-relative paths the detector's own contract exempts -- the
    chokepoint module that legitimately owns the pattern it bans. Constant
    ``detector_kwargs`` are forwarded, and ``kwargs_for`` supplies the per-module
    ones (a detector that allows a named function in one specific module), so a
    parameterized detector is swept through here like one taking nothing instead
    of each such test hand-rolling the loop and re-deciding which path holds the
    definition.
    """
    exempt = {str(item) for item in skip}
    offenders: list[str] = []
    for path, tree in app_trees(exclude=exclude):
        relative = relative_posix(path)
        if relative in exempt:
            continue
        per_path = kwargs_for(relative) if kwargs_for is not None else {}
        offenders.extend(detector(tree, path, **detector_kwargs, **per_path))
    return offenders
