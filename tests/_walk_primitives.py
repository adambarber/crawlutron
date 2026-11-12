"""Walk primitives: scopes, parents, line numbers and offenders."""

import ast
import hashlib
import io
import pathlib
import tokenize
import weakref
from functools import cache

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string Constant with its line, including f-string literal fragments."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.append((value.lineno, value.value))
    return found


def comment_lines(source: str) -> list[tuple[int, str]]:
    """Every comment token as ``(line, text)`` (tokenize: a # in a string is not one)."""
    return [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]


REPO_ROOT = TESTS_DIR.parent


@cache
def python_bearing_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Every top-level dir under ``base`` holding any ``*.py``, sorted. Hidden dirs and symlinked trees
    (venvs) are skipped -- caches and environments, not shipped code. Derived by walking, never
    hand-listed: the app/ rename and crawlutron_sdk's late arrival were hand-list blindness.
    """
    roots = [
        d.name
        for d in base.iterdir()
        if d.is_dir()
        and not d.is_symlink()
        and not d.name.startswith(".")
        and any("__pycache__" not in q.parts for q in d.rglob("*.py"))
    ]
    assert roots, f"no python-bearing roots under {base}: repo surface is empty"
    return tuple(sorted(roots))


SCAN_ROOTS = python_bearing_roots()


APP_ROOT = REPO_ROOT / "crawlutron"


TRANSPORT_DIR = APP_ROOT / "transport"


# The node sequence of every root any detector has walked, keyed by the root's identity.
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = weakref.WeakKeyDictionary()


# The same sequence, narrowed to one node kind, keyed by kind then root.
_NODES_BY_KIND: weakref.WeakKeyDictionary[ast.AST, dict[str, tuple[ast.AST, ...]]] = (
    weakref.WeakKeyDictionary()
)


# One AST per (path, source digest) per session; keyed on content, never (mtime, size), so a rewrite
# that preserves size and mtime is re-parsed from fresh bytes, never stale.
_PARSE_CACHE: dict[tuple[str, str], ast.Module] = {}


def parsed(path: pathlib.Path) -> ast.Module:
    """The AST of `path`, parsed once per session and shared. Keyed on the source's SHA-256 so a rewritten
    file -- even one unchanged in size and mtime -- is re-parsed from fresh bytes, never stale. A
    nonexistent path raises here -- the ONE CACHED seam every cached reader uses.
    """
    if not path.is_file():
        raise FileNotFoundError(f"ast requested for a nonexistent source file: {path}")
    source = path.read_text(encoding="utf-8")
    key = (str(path), hashlib.sha256(source.encode()).hexdigest())
    tree = _PARSE_CACHE.get(key)
    if tree is None:
        tree = ast.parse(source, filename=str(path))
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
def declared_root_files(root: str) -> tuple[pathlib.Path, ...]:
    """Every .py file under a declared repo-relative scan root; missing/empty FAILS LOUD. A gate iterating a
    nonexistent root is silently vacuous -- the app/ rename made the comment gate blind for months -- so a
    root resolving to nothing is a gate defect, never a green.
    """
    base = REPO_ROOT / root
    if not base.is_dir():
        raise AssertionError(f"declared scan root does not exist: {root}/ (gate would scan nothing)")
    files = tuple(sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts))
    if not files:
        raise AssertionError(f"declared scan root contains no .py files: {root}/ (gate would scan nothing)")
    return files


@cache
def _trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return tuple((path, parsed(path)) for path in sorted(directory.rglob("*.py")))


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. crawlutron/service/webhooks.py)."""
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
