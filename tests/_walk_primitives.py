import ast
import pathlib
import weakref
from functools import cache

TESTS_DIR = pathlib.Path(__file__).resolve().parent


REPO_ROOT = TESTS_DIR.parent


APP_ROOT = REPO_ROOT / "app"


TRANSPORT_DIR = APP_ROOT / "transport"


# The node sequence of every root any detector has walked, keyed by the root's identity.
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = weakref.WeakKeyDictionary()


# The same sequence, narrowed to one node kind, keyed by kind then root.
_NODES_BY_KIND: weakref.WeakKeyDictionary[ast.AST, dict[str, tuple[ast.AST, ...]]] = (
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


# ``id(node) -> parent`` for every node of one root, keyed by the root's identity on the same weak terms as
# _NODES_BY_ROOT.
_PARENTS_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, dict[int, ast.AST]] = weakref.WeakKeyDictionary()


def enclosing_function(
    node: ast.AST, parents: dict[int, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function that encloses `node`, or ``None`` if it is at module scope.

    :func:`parent_map` one level up. Every "which function is this call
    inside?" detector wants it, and it was a three-line loop written into each
    one -- which is how two detectors end up disagreeing about whether a nested
    function counts as the owner, since a copy in each place was free to settle
    the question differently.
    """
    owner = parents.get(id(node))
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents.get(id(owner))
    return owner


def parent_map(root: ast.AST) -> dict[int, ast.AST]:
    """``id(node) -> parent`` for every node in `root`, built once per root.

    :func:`nodes_of` one level down, for the same reason: it is a pure function
    of a tree this module already snapshots, and the detectors were recomputing
    it per candidate node instead of per tree.

    Keyed by ``id`` because that is the question being asked -- "what is this
    exact node's parent" -- and :class:`ast.AST` hashes by identity anyway, so
    no structural key would be both cheaper and clearer. The map holds ids
    rather than nodes, which is safe precisely because the weak key does the
    holding: while the entry lives, the root is alive, and the root reaches
    every one of its descendants, so no id in the map can be recycled onto an
    unrelated node.
    """
    cached = _PARENTS_BY_ROOT.get(root)
    if cached is None:
        cached = {id(child): parent for parent in nodes_of(root) for child in ast.iter_child_nodes(parent)}
        _PARENTS_BY_ROOT[root] = cached
    return cached


def lineno(node: ast.AST) -> int:
    """The source line of `node`.

    ``ast.AST`` is the root of the hierarchy and declares only ``_fields`` and
    ``_attributes``; ``lineno`` is declared on the subclasses (``stmt``,
    ``expr``, ``pattern``, ...). So reading it off a walk result is a claim about
    which kind of node the walk produced, and the type system is right to refuse
    the claim rather than take it on faith.

    The claim holds for every node ``ast.parse`` produces, which is all any
    detector here ever holds, so this is the one place that gets to state it.
    What was not previously true is that a false claim would be noticed: an
    unchecked read raised ``AttributeError`` from inside a detector, which reads
    as a broken detector instead of a broken call site, and a detector that
    raises is one whose verdict nobody trusts.
    """
    line = getattr(node, "lineno", None)
    if not isinstance(line, int):
        raise TypeError(f"{type(node).__name__} carries no lineno")
    return line


def _narrow(root: ast.AST, kind: str, *types: type[ast.AST]) -> tuple[ast.AST, ...]:
    """`nodes_of` filtered to `types`, cached under `kind`.

    Returns ``tuple[ast.AST, ...]`` because one cache cannot carry a different
    element type per key without a TypeVar. Each public walker below therefore
    repeats the isinstance test to earn its precise return type. That second
    test is redundant work and is kept on purpose: it is the check that makes
    the annotation true rather than asserted, and it costs one isinstance over a
    tuple that is already built and already walked.
    """
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


def offender_at(path: pathlib.Path, node: ast.AST, *detail: str) -> str:
    """``offender`` reading the line number off the node, from message parts.

    Every detector in the tree ends by appending one of these, and the
    three-argument ``offender(path, node.lineno, ...)`` spelling makes the call
    wrap as soon as the message is longer than the line budget -- so the shape
    that is *most* common (a message explaining why the shape is wrong) is
    exactly the shape that stops being one line, and a wrapped detector is a
    seven-line block that duplicates every other wrapped detector. jscpd
    reported twelve of them as clones, which is the same fact this exists to
    stop: the duplication was the wrapping, not the logic.

    Taking the parts separately and joining them here keeps the append on one
    line at any message length, and ``offender`` stays the one place the label
    is formatted.
    """
    return offender(path, lineno(node), "".join(detail))
