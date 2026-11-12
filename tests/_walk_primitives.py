"""Walk primitives: scopes, parents, line numbers and offenders."""

import ast
import hashlib
import io
import pathlib
import tokenize
import tomllib
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


def uv_workspace_members(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...] | None:
    """``[tool.uv.workspace].members`` from the root pyproject, or None when absent.

    The packaging truth's key: when a UV workspace declares members, PACKAGE_ROOTS
    derives from them; until then the current-layout fallback stands."""
    pyproject = base / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as handle:
            table = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return None
    tool = table.get("tool")
    if not isinstance(tool, dict):
        return None
    workspace = tool.get("uv", {}).get("workspace") if isinstance(tool.get("uv"), dict) else None
    if not isinstance(workspace, dict):
        return None
    members = workspace.get("members")
    if not isinstance(members, list):
        return None
    return tuple(str(m) for m in members if isinstance(m, str))


def _member_package_roots(base: pathlib.Path, members: tuple[str, ...]) -> tuple[str, ...]:
    """The python-bearing member dirs of a UV workspace, named repo-relatively.

    A member that holds any ``*.py`` is its own package root; a member with none
    is a stub (docs-only), not part of the packaging surface."""
    roots = [
        member
        for member in members
        if any("__pycache__" not in q.parts for q in (base / member).rglob("*.py"))
        if (base / member).is_dir()
    ]
    assert roots, f"uv workspace members declare no python-bearing dirs under {base}: {members}"
    return tuple(sorted(roots))


def package_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """The importable package dirs (derived packaging truth), sorted.

    A UV workspace declaration keys it; without one, the current-layout fallback
    (every top-level python-bearing dir) stands until the Wave A manifest arrives."""
    members = uv_workspace_members(base)
    if members is not None:
        return _member_package_roots(base, members)
    return python_bearing_roots(base)


#: The importable package dirs. SCAN_ROOTS consumes this, so a Wave A manifest
#: swap-in re-derives both here, once.
PACKAGE_ROOTS = package_roots()


def scan_roots(base: pathlib.Path = REPO_ROOT) -> tuple[str, ...]:
    """Every python-bearing dir under ``base``, computed through PACKAGE_ROOTS.

    The scan surface is the package roots plus non-package python-bearing top-level
    dirs (tests, scripts); SCAN_ROOTS consumes PACKAGE_ROOTS, one re-derivation."""
    packages = set(PACKAGE_ROOTS) if base == REPO_ROOT else set(package_roots(base))
    return tuple(sorted(packages | set(python_bearing_roots(base))))


SCAN_ROOTS = scan_roots()


def is_exempt(relative: str, value: str, exemptions: tuple[tuple[str, str, str], ...]) -> bool:
    """True if a (path-pattern, allowed-substring) exemption row covers ``value``.
    Shared by the string-drift invariant gates so their ``_exempt`` is one home."""
    for pattern, allowed, _justification in exemptions:
        path = pathlib.PurePosixPath(relative)
        if path == pathlib.PurePosixPath(pattern) or path.match(pattern):
            if allowed in value:
                return True
    return False


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
