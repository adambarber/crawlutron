"""Walk primitives: scopes, parents, line numbers and offenders."""

import ast
import copy
import hashlib
import io
import os
import pathlib
import shutil
import subprocess
import tokenize
import weakref
from dataclasses import dataclass
from functools import cache

from utils.paths import (
    ADMISSION_ROOT,
    APP_ROOT,
    APP_ROOTS,
    MCP_ROOT,
    PACKAGE_ROOTS,
    REPO_ROOT,
    REST_ROOT,
    SAFE_URL_ROOT,
    SCAN_ROOTS,
    TESTS_DIR,
    TRANSPORT_DIR,
    get_test_roots,
    import_roots,
    is_exempt,
    member_import_names,
    member_src_roots,
    package_roots,
    path_components,
    prepend_member_roots,
    python_bearing_roots,
    relative_posix,
    scan_roots,
    uv_workspace_members,
    workspace_package_dirs,
    workspace_project,
)

__all__ = [
    "ADMISSION_ROOT",
    "canonical_tree",
    "APP_ROOT",
    "APP_ROOTS",
    "MCP_ROOT",
    "PACKAGE_ROOTS",
    "REST_ROOT",
    "REPO_ROOT",
    "SAFE_URL_ROOT",
    "SCAN_ROOTS",
    "TESTS_DIR",
    "TRANSPORT_DIR",
    "get_test_roots",
    "import_roots",
    "is_exempt",
    "member_import_names",
    "member_src_roots",
    "package_roots",
    "path_components",
    "prepend_member_roots",
    "python_bearing_roots",
    "relative_posix",
    "scan_roots",
    "uv_workspace_members",
    "workspace_package_dirs",
    "workspace_project",
    "discovered_files",
]


# Directories never descended into during file discovery.
_SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "work", ".hardstand", "node_modules", ".pytest_cache", "__pycache__"}
)


@cache
def discovered_files(root: pathlib.Path, pattern: str) -> tuple[pathlib.Path, ...]:
    """Git-tracked files under ``root`` matching ``pattern``; skip dirs pruned."""
    keep: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRECTORIES]
        for name in filenames:
            path = pathlib.Path(dirpath) / name
            if path.match(pattern):
                keep.append(path)
    if not (root / ".git").exists():
        return tuple(sorted(keep))
    git = shutil.which("git") or "git"
    result = subprocess.run(
        [git, "-C", str(root), "ls-files", pattern],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return tuple(sorted(keep))
    tracked = set(result.stdout.splitlines())
    return tuple(sorted(p for p in keep if p.relative_to(root).as_posix() in tracked))


@dataclass(frozen=True)
class CacheMetadata:
    """Metadata attached to each cached AST copy."""

    source_sha256: str
    ast_schema_version: str
    validation_result: str


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


# The node sequence of every root any detector has walked, keyed by the root's identity.
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = weakref.WeakKeyDictionary()


# The same sequence, narrowed to one node kind, keyed by kind then root.
_NODES_BY_KIND: weakref.WeakKeyDictionary[ast.AST, dict[str, tuple[ast.AST, ...]]] = (
    weakref.WeakKeyDictionary()
)


# One canonical AST per (path, source digest); keyed on content, never (mtime, size).
# Rewrite preserving size/mtime re-parses from fresh bytes.
_PARSE_CACHE: dict[tuple[str, str], tuple[ast.Module, CacheMetadata]] = {}

# AST schema version - increment when AST structure expectations change
_AST_SCHEMA_VERSION = "3.14"


def _validate_ast(tree: ast.Module) -> str:
    """Validate the parsed AST. Returns 'valid' or an error description."""
    try:
        # Basic validation: ensure it's a well-formed module
        ast.fix_missing_locations(tree)
        compile(tree, filename=tree.__dict__.get("filename", "<unknown>"), mode="exec")
        return "valid"
    except Exception as e:
        return f"invalid: {e}"


def parsed_source(source: str, filename: str = "<module>") -> ast.Module:
    """The single parse entry for source strings, including synthetic gate probes."""
    return ast.parse(source, filename=filename)


def _canonical_pair(path: pathlib.Path) -> tuple[ast.Module, CacheMetadata]:
    """Parse once per (path, digest); metadata lives on the canonical tree."""
    if not path.is_file():
        raise FileNotFoundError(f"ast requested for a nonexistent source file: {path}")
    source = path.read_text(encoding="utf-8")
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    key = (str(path), source_sha256)
    cached = _PARSE_CACHE.get(key)
    if cached is None:
        tree = parsed_source(source, str(path))
        metadata = CacheMetadata(
            source_sha256=source_sha256,
            ast_schema_version=_AST_SCHEMA_VERSION,
            validation_result=_validate_ast(tree),
        )
        object.__setattr__(
            tree,
            "_cache_metadata",
            {
                "source_sha256": metadata.source_sha256,
                "ast_schema_version": metadata.ast_schema_version,
                "validation_result": metadata.validation_result,
            },
        )
        _PARSE_CACHE[key] = (tree, metadata)
        cached = _PARSE_CACHE[key]
    return cached


def canonical_tree(path: pathlib.Path) -> ast.Module:
    """The one shared read-only AST for `path`; callers must not mutate it."""
    return _canonical_pair(path)[0]


def _parsed_copy(path: pathlib.Path) -> ast.Module:
    """Private copy door for mutation-boundary tests; readers use canonical_tree."""
    return copy.deepcopy(canonical_tree(path))


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


def _walk_root_files(base: pathlib.Path, root: str) -> tuple[pathlib.Path, ...]:
    """Every .py under ``base / root`` (no ``__pycache__``), raising loud on a missing dir.

    The one per-root walk both declared_root_files and empty_scan_roots share; a gate
    walking nothing must fail loud, never green."""
    directory = base / root
    if not directory.is_dir():
        raise AssertionError(f"declared scan root does not exist: {root}/ (base {base})")
    return tuple(sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts))


@cache
def declared_root_files(root: str) -> tuple[pathlib.Path, ...]:
    """Every .py file under a declared repo-relative scan root; missing/empty FAILS LOUD. A gate iterating a
    nonexistent root is silently vacuous -- the app/ rename made the comment gate blind for months -- so a
    root resolving to nothing is a gate defect, never a green.
    """
    files = _walk_root_files(REPO_ROOT, root)
    if not files:
        raise AssertionError(f"declared scan root contains no .py files: {root}/ (gate would scan nothing)")
    return files


def empty_scan_roots(roots: tuple[str, ...], base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Declared roots resolving to zero .py files under ``base``: the vacuity failure, named.

    A dead root would green whatever gate walks it, so it is the walk-vacuity gate's
    finding; a missing root dir raises instead of returning empty, itself a gate defect."""
    empty: list[str] = []
    for root in roots:
        if not _walk_root_files(base, root):
            empty.append(root)
    return tuple(empty)


@cache
def _trees_under(directory: pathlib.Path) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return tuple((path, canonical_tree(path)) for path in sorted(directory.rglob("*.py")))


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. crawlutron/service/webhooks.py)."""
    path = REPO_ROOT / relative
    return path, canonical_tree(path)


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
