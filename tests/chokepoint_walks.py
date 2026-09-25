"""Shared AST-walk helpers for the chokepoint invariant (drift) tests.

Each safety chokepoint -- safe_regex, bounded_io, BoundedTTLCache, SafeUrl,
env parsing -- is guarded by an invariant test that walks app/*.py, parses
every module, and flags a banned pattern. The file walk, the parse loop, the
safe_regex.py exemption, and the offender-label format are the same across
all of them; they live here so the enforcement tests cannot drift apart over
them. The detectors themselves stay next to the contract they pin (in the
test files); this module only removes the repeated ~10-line walk scaffold per
test that otherwise invites the exemptions and labels to drift.

ONE traversal, and it is executed once. That is the part this module owns, and
it was the part it did not do: the helpers re-globbed, re-read and re-parsed all
105 app/ modules on every call, so one tests/test_safety_invariants.py run paid
for the same traversal about 44 times over. Measured per traversal: 0.0018s to
glob and 0.1121s to read and parse 105 modules, so 0.114s each, and 5.4s of that
file's run (interleaved before/after: 47.0s -> 41.6s) plus 25.5s of a full sweep
(132.4s -> 106.9s), because other files walk app/ too. Its seven slow detectors,
37.7s between them, are their OWN per-node analysis and this does not touch
them: what is removed is the traversal underneath them, not the analysis.

The cost was the smaller half of the defect. Because each walk observed the tree
at a different instant, "list the files" and "read the files" were separate
moments, and a file that appeared or vanished between them raised FileNotFoundError
inside a detector rather than being reported -- the list-then-read race that
tests/test_jscpd_gate.py caused whenever it planted a probe in app/ beside a
chokepoint sweep. One snapshot per exclude set fixes both halves at once, and the
detectors share the same ast.Module objects rather than equal copies of them: a
re-parse compares equal everywhere a detector looks, so only identity
distinguishes "I re-read the tree" from "the tree changed".

Keeping this pure-stdlib means the static invariant tests do not pay the
heavy ``helpers`` -> engine -> settings import chain.
"""

from __future__ import annotations

import ast
import pathlib
import weakref
from collections.abc import Iterator
from functools import cache, lru_cache

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
APP_ROOT = REPO_ROOT / "app"
STRATEGIES_DIR = APP_ROOT / "crawl" / "strategies"

# app/safe_regex.py is the sanctioned regex wrapper (stdlib `re` for
# literals; the third-party `regex` engine for bounded execution). It owns
# the banned primitives, so a full-tree walk excludes it. A test that must
# also exempt its own chokepoint module passes `exclude=SANCTIONED | {"..."}`.
SANCTIONED = frozenset({"safe_regex.py"})

# The node sequence of every root any detector has walked, keyed by the root's
# identity. Weak so a synthetic tree a property test built for one assertion is
# released with the test that built it, rather than pinned for the session: a
# strong dict here would grow with every planted tree and hold a whole parsed
# module per case. `ast.AST` supports weak references, and identity is the
# right key -- a re-parse of an unchanged file compares equal everywhere a
# detector looks, so only identity distinguishes "I already walked this" from
# "this is a different tree that happens to look the same".
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = (
    weakref.WeakKeyDictionary()
)


def nodes_of(root: ast.AST) -> tuple[ast.AST, ...]:
    """``tuple(ast.walk(root))``, materialized once per root and shared.

    a962c7b removed the re-GLOB and the re-READ underneath the detectors. The
    re-WALK is the same defect one level deeper and it is the larger half: this
    tree holds 40 detectors and 91 ``ast.walk`` sites over ~105 modules, so
    every detector re-walked every AST -- forty passes over every node where
    one would do. Sub-walks (a detector narrowing to one function and walking
    that) go through here too, and they are where the sharing pays most: the
    same function body is walked by many detectors, and a key that fits in one
    dictionary serves all of them.

    The sequence is byte-for-byte what ``ast.walk`` yields -- same objects, same
    breadth-first order -- so a detector cannot tell the difference, which is
    the property ``test_every_detector_sees_the_same_nodes`` asserts by
    execution. A detector that needs a fresh walk of a mutated tree must not
    mutate one: the snapshots above are already frozen, and this makes the
    freeze uniform instead of per-detector.
    """
    cached = _NODES_BY_ROOT.get(root)
    if cached is None:
        cached = tuple(ast.walk(root))
        _NODES_BY_ROOT[root] = cached
    return cached


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


def app_offenders(detector, *, exclude: frozenset[str] = SANCTIONED) -> list[str]:
    """Run one app-tree detector and collect its formatted offenders."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=exclude):
        offenders.extend(detector(tree, path))
    return offenders


def calls_named(tree: ast.AST, function_name: str) -> Iterator[ast.Call]:
    """Yield direct calls to one named function in an AST tree."""
    for node in nodes_of(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == function_name
        ):
            yield node


def call_keyword_values(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
) -> Iterator[tuple[ast.Call, ast.expr]]:
    """Yield selected keyword values from direct calls to named functions."""
    names = {function_names} if isinstance(function_names, str) else function_names
    keywords = {keyword_names} if isinstance(keyword_names, str) else set(keyword_names)
    for node in nodes_of(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in names
        ):
            continue
        # One pair per DISTINCT keyword name: Python itself rejects a repeated
        # keyword, so a call carrying two is un-compilable source, and emitting
        # an offender per occurrence would over-report against the one-per-call
        # behavior these detectors had before they were unified.
        seen_keywords: set[str | None] = set()
        for keyword in node.keywords:
            if keyword.arg in keywords and keyword.arg not in seen_keywords:
                seen_keywords.add(keyword.arg)
                yield node, keyword.value


def trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """``(path, tree)`` for every module under ``directory``, walked once.

    The same ownership as ``app_trees``, and for the same reason: the
    detectors that sweep the tests/ tree all read it, and a tree that is
    re-read per detector is a tree each of them observed separately.
    """
    return _trees_under(directory)


@cache
def _trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(directory.rglob("*.py"))
    )


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. app/api/webhooks.py)."""
    path = REPO_ROOT / relative
    return path, ast.parse(path.read_text(encoding="utf-8"))


def offender(path: pathlib.Path, lineno: int, detail: str = "") -> str:
    """Format an offender as ``app/pkg/mod.py:lineno detail`` for messages."""
    label = f"{path.relative_to(REPO_ROOT)}:{lineno}"
    return f"{label} {detail}".strip()


def _static_string(expr: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _static_string(expr.left, bindings)
        right = _static_string(expr.right, bindings)
        return left + right if left is not None and right is not None else None
    if isinstance(expr, ast.JoinedStr):
        parts = [_static_string(value, bindings) for value in expr.values]
        if all(part is not None for part in parts):
            return "".join(parts)
    return None


@lru_cache(maxsize=256)
def _import_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    nodes = nodes_of(tree)
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                bindings[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name if alias.asname else local
    assignments = [node for node in nodes if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            canonical = "" if isinstance(value, ast.Call) else _resolved_expr(value, bindings)
            canonical = canonical or _static_string(value, bindings)
            if not canonical:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bindings:
                    bindings[target.id] = canonical
                    changed = True
    return bindings


def _resolved_expr(expr: ast.AST, bindings: dict[str, str]) -> str:
    """Resolve import aliases and constant-folded ``getattr`` names.

    Only literals, concatenations, and constant f-strings are folded for an
    attribute name. Runtime-computed values remain opaque by design.
    """
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolved_expr(expr.value, bindings)
        return f"{base}.{expr.attr}" if base else expr.attr
    if isinstance(expr, ast.Call):
        callee = _resolved_expr(expr.func, bindings)
        if callee.rsplit(".", 1)[-1] == "getattr" and len(expr.args) >= 2:
            attribute = _static_string(expr.args[1], bindings)
            base = _resolved_expr(expr.args[0], bindings)
            if attribute is not None and base:
                return f"{base}.{attribute}"
        return callee
    return ""


def resolved_expr_name(tree: ast.AST, expr: ast.AST) -> str:
    """Return an expression's import-resolved dotted name for AST detectors."""
    return _resolved_expr(expr, _import_bindings(tree))


def resolved_call_name(tree: ast.AST, call: ast.Call) -> str:
    """Return a call's import-resolved dotted name for AST detectors."""
    return resolved_expr_name(tree, call)
