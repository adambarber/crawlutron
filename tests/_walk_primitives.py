"""Walk primitives: scopes, parents, line numbers and offenders."""

import ast
import hashlib
import os
import pathlib
import sys
import weakref
from functools import cache

TESTS_DIR = pathlib.Path(__file__).resolve().parent


REPO_ROOT = TESTS_DIR.parent


APP_ROOT = REPO_ROOT / "crawlutron"


TRANSPORT_DIR = APP_ROOT / "transport"


# The node sequence of every root any detector has walked, keyed by the root's identity.
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = weakref.WeakKeyDictionary()


# The same sequence, narrowed to one node kind, keyed by kind then root.
_NODES_BY_KIND: weakref.WeakKeyDictionary[ast.AST, dict[str, tuple[ast.AST, ...]]] = (
    weakref.WeakKeyDictionary()
)


# One parse per (path, mtime_ns, size, py_version) per session; every tree reader funnels here.
_PARSE_CACHE: dict[tuple[str, int, int, str], ast.Module] = {}

# Persistent parse cache under .pytest_cache/ast_parse/, keyed on
# (path, mtime_ns, size, py_version); stale on edit or Python version bump.
_AST_PARSE_CACHE_DIR = REPO_ROOT / ".pytest_cache" / "ast_parse"
_PY_VERSION = ".".join(map(str, sys.version_info[:3]))


def _cache_file(path: pathlib.Path, mtime_ns: int, size: int) -> pathlib.Path:
    """Cache-file path for one parse key, hashed to a filesystem-safe name."""
    digest = hashlib.sha256(f"{path}\0{mtime_ns}\0{size}\0{_PY_VERSION}".encode()).hexdigest()
    return _AST_PARSE_CACHE_DIR / f"{digest}.src"


def _read_source(path: pathlib.Path, stat: os.stat_result) -> str:
    """Source for `path`, served from the persistent parse cache when present.

    The cache file is keyed on (path, mtime_ns, size, py_version); a miss reads
    the source file and populates the cache so the next session reuses it.
    """
    _AST_PARSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = _cache_file(path, stat.st_mtime_ns, stat.st_size)
    try:
        return cf.read_text(encoding="utf-8")
    except FileNotFoundError:
        source = path.read_text(encoding="utf-8")
        cf.write_text(source, encoding="utf-8")
        return source


def parsed(path: pathlib.Path) -> ast.Module:
    """The AST of `path`, parsed once per session and shared.

    Keyed on mtime, size and Python version so an edited file is re-parsed,
    never stale: a detector reading the cache still sees a freshly planted
    violation. The source is persisted under .pytest_cache/ast_parse/ so
    repeat sessions reuse it instead of re-reading the source file.
    """
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, _PY_VERSION)
    tree = _PARSE_CACHE.get(key)
    if tree is None:
        tree = ast.parse(_read_source(path, stat), filename=str(path))
        _PARSE_CACHE[key] = tree
    return tree


def nodes_of(root: ast.AST) -> tuple[ast.AST, ...]:
    """``tuple(ast.walk(root))``, materialized once per root and shared.

    Without it 40 detectors re-walked every AST; sub-walks served by the same
    cache share most of all. Byte-for-byte what ``ast.walk`` yields."""
    cached = _NODES_BY_ROOT.get(root)
    if cached is None:
        cached = tuple(ast.walk(root))
        _NODES_BY_ROOT[root] = cached
    return cached


# ``id(node) -> parent`` for every node of one root, keyed by the root's identity on the same weak terms as
# _NODES_BY_ROOT.
_PARENTS_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, dict[int, ast.AST]] = weakref.WeakKeyDictionary()


def enclosing_function(
    node: ast.AST, parents: dict[int, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function that encloses `node`, or ``None`` if it is at module scope.

    :func:`parent_map` one level up; every "which function is this call in"
    detector wants it, and it was a three-line loop per detector."""
    owner = parents.get(id(node))
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents.get(id(owner))
    return owner


def parent_map(root: ast.AST) -> dict[int, ast.AST]:
    """``id(node) -> parent`` for every node in `root`, built once per root.

    :func:`nodes_of` one level down, keyed by ``id`` because that IS the
    question (``ast.AST`` hashes by identity anyway)."""
    cached = _PARENTS_BY_ROOT.get(root)
    if cached is None:
        cached = {id(child): parent for parent in nodes_of(root) for child in ast.iter_child_nodes(parent)}
        _PARENTS_BY_ROOT[root] = cached
    return cached


def lineno(node: ast.AST) -> int:
    """The source line of `node`.

    ``lineno`` lives on the ``expr``/``stmt`` subclasses, not on ``ast.AST``,
    so the one place that parses trees gets to claim it."""
    line = getattr(node, "lineno", None)
    if not isinstance(line, int):
        raise TypeError(f"{type(node).__name__} carries no lineno")
    return line


def _narrow(root: ast.AST, kind: str, *types: type[ast.AST]) -> tuple[ast.AST, ...]:
    """`nodes_of` filtered to `types`, cached under `kind`.

    Returns the base type because one cache cannot carry an element type per
    key; each walker re-asserts the isinstance to earn its precise return."""
    by_kind = _NODES_BY_KIND.get(root)
    if by_kind is None:
        by_kind = {}
        _NODES_BY_KIND[root] = by_kind
    found = by_kind.get(kind)
    if found is None:
        found = tuple(node for node in nodes_of(root) if isinstance(node, types))
        by_kind[kind] = found
    return found


def trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """``(path, tree)`` for every module under ``directory``, walked once.

    The same ownership as ``app_trees``: a tree re-read per detector is a
    tree each of them observed separately."""
    return _trees_under(directory)


@cache
def _trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return tuple((path, parsed(path)) for path in sorted(directory.rglob("*.py")))


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. crawlutron/api/webhooks.py)."""
    path = REPO_ROOT / relative
    return path, parsed(path)


def offender(path: pathlib.Path, lineno: int, detail: str = "") -> str:
    """Format an offender as ``crawlutron/pkg/mod.py:lineno detail`` for messages."""
    label = f"{path.relative_to(REPO_ROOT)}:{lineno}"
    return f"{label} {detail}".strip()


def offender_at(path: pathlib.Path, node: ast.AST, *detail: str) -> str:
    """``offender`` reading the line number off the node, from message parts.

    The three-argument spelling wraps exactly when the message is longest, so
    twelve wrapped detectors read as clones; joining here keeps them one line."""
    return offender(path, lineno(node), "".join(detail))


def attribute_call_on(call: ast.Call, obj: str, attr: str) -> bool:
    """True if ``call`` is ``<obj>.<attr>(...)``."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == attr
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == obj
    )
