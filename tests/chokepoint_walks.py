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
105 app/ modules on every call, so one tests/invariants/ run paid
for the same traversal about 44 times over. Measured per traversal: 0.0018s to
glob and 0.1121s to read and parse 105 modules, so 0.114s each, and 5.4s of that
file's run (interleaved before/after: 47.0s -> 41.6s) plus 25.5s of a full sweep
(132.4s -> 106.9s), because other files walk app/ too.

The cost was the smaller half of the defect. Because each walk observed the tree
at a different instant, "list the files" and "read the files" were separate
moments, and a file that appeared or vanished between them raised FileNotFoundError
inside a detector rather than being reported -- the list-then-read race that
tests/test_jscpd_gate.py caused whenever it planted a probe in app/ beside a
chokepoint sweep. One snapshot per exclude set fixes both halves at once, and the
detectors share the same ast.Module objects rather than equal copies of them: a
re-parse compares equal everywhere a detector looks, so only identity
distinguishes "I re-read the tree" from "the tree changed".

ONE node sequence per root, which is the same argument one level down. Owning
the snapshot removed the re-GLOB and the re-READ; the re-WALK underneath it was
still there, and it was the larger half. ``nodes_of`` below is that fix, and the
seven detectors left slow after the first one were slow because of it: they were
walking the same tree forty times over, not because their own analysis was
expensive. What is removed is the repeated walk, never the analysis -- the
detectors' bodies are unchanged, and the receipt for that is
``tests/invariants/``, which compares every detector's offender
set before and after and finds them identical.

Keeping this pure-stdlib means the static invariant tests do not pay the
heavy ``helpers`` -> engine -> settings import chain.
"""

from __future__ import annotations

import ast
import pathlib
import tokenize
import weakref
from collections.abc import Callable, Container, Iterable, Iterator
from functools import cache, lru_cache
from typing import TypeGuard

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
_NODES_BY_ROOT: weakref.WeakKeyDictionary[ast.AST, tuple[ast.AST, ...]] = weakref.WeakKeyDictionary()

# The same sequence, narrowed to one node kind, keyed by kind then root. Split
# out from _NODES_BY_ROOT because the value's element type depends on the kind,
# and a mapping whose value type depends on its key is a TypeVar wearing a
# table.
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


# The narrow walks below are a detector's vocabulary, named for what a rule asks
# of the tree rather than for the ast class that happens to answer it. Each
# returns the node kind it filtered to, so a detector reads node.func / node.args
# / node.value without re-asserting what the walk already proved.
def call_nodes(root: ast.AST) -> tuple[ast.Call, ...]:
    """Every call in `root`, in walk order. The busiest walk in the suite."""
    return tuple(node for node in _narrow(root, "call", ast.Call) if isinstance(node, ast.Call))


def attribute_reads(root: ast.AST) -> tuple[ast.Attribute, ...]:
    """Every ``obj.attr`` read. What a rule about member access walks."""
    return tuple(
        node for node in _narrow(root, "attribute", ast.Attribute) if isinstance(node, ast.Attribute)
    )


def name_reads(root: ast.AST) -> tuple[ast.Name, ...]:
    """Every bare name reference. What a rule about identifier use walks."""
    return tuple(node for node in _narrow(root, "name", ast.Name) if isinstance(node, ast.Name))


def string_literals(root: ast.AST) -> tuple[ast.Constant, ...]:
    """Every ``ast.Constant``: string, number, bool and ``None`` alike."""
    return tuple(node for node in _narrow(root, "constant", ast.Constant) if isinstance(node, ast.Constant))


def subscript_reads(root: ast.AST) -> tuple[ast.Subscript, ...]:
    """Every ``a[b]``. Indexed access, which attribute reads do not see."""
    return tuple(
        node for node in _narrow(root, "subscript", ast.Subscript) if isinstance(node, ast.Subscript)
    )


def dict_displays(root: ast.AST) -> tuple[ast.Dict, ...]:
    """Every ``{...}`` dict display, as opposed to a comprehension."""
    return tuple(node for node in _narrow(root, "dict", ast.Dict) if isinstance(node, ast.Dict))


def class_defs(root: ast.AST) -> tuple[ast.ClassDef, ...]:
    """Every class definition."""
    return tuple(node for node in _narrow(root, "class", ast.ClassDef) if isinstance(node, ast.ClassDef))


def try_blocks(root: ast.AST) -> tuple[ast.Try, ...]:
    """Every ``try`` statement -- not ``TryStar``, whose handlers differ."""
    return tuple(node for node in _narrow(root, "try", ast.Try) if isinstance(node, ast.Try))


def returns(root: ast.AST) -> tuple[ast.Return, ...]:
    """Every ``return``. What a rule about a function's exit walks."""
    return tuple(node for node in _narrow(root, "return", ast.Return) if isinstance(node, ast.Return))


def unary_ops(root: ast.AST) -> tuple[ast.UnaryOp, ...]:
    """Every unary expression: ``not x``, ``-x``, ``~x``."""
    return tuple(node for node in _narrow(root, "unary", ast.UnaryOp) if isinstance(node, ast.UnaryOp))


def f_strings(root: ast.AST) -> tuple[ast.JoinedStr, ...]:
    """Every f-string, as one node however many holes it interpolates."""
    return tuple(node for node in _narrow(root, "fstring", ast.JoinedStr) if isinstance(node, ast.JoinedStr))


def callable_defs(root: ast.AST) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ...]:
    """Everything a rule can call by name: both def forms and lambdas."""
    return tuple(
        node
        for node in _narrow(root, "callable", ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    )


def writes(root: ast.AST) -> tuple[ast.Assign | ast.AnnAssign | ast.AugAssign, ...]:
    """Every statement that binds a name. `assignments` turns these into triples."""
    return tuple(
        node
        for node in _narrow(root, "write", ast.Assign, ast.AnnAssign, ast.AugAssign)
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign)
    )


def import_statements(root: ast.AST) -> tuple[ast.Import | ast.ImportFrom, ...]:
    """Every import, plain and from-form alike, in walk order."""
    return tuple(
        node
        for node in _narrow(root, "import", ast.Import, ast.ImportFrom)
        if isinstance(node, ast.Import | ast.ImportFrom)
    )


def plain_imports(root: ast.AST) -> tuple[ast.Import, ...]:
    """``import x`` only, split from the from-form because the module a tree
    pulls in is the first part of the alias here, not ``node.module``."""
    return tuple(node for node in _narrow(root, "plain-import", ast.Import) if isinstance(node, ast.Import))


def from_imports(root: ast.AST) -> tuple[ast.ImportFrom, ...]:
    """``from x import y`` only, the other half of the split above."""
    return tuple(
        node for node in _narrow(root, "from-import", ast.ImportFrom) if isinstance(node, ast.ImportFrom)
    )


def unannotated_writes(root: ast.AST) -> tuple[ast.Assign | ast.AugAssign, ...]:
    """Writes that bind without declaring a type: ``=`` and ``+=``.

    `assignments` covers the three statement forms and yields the target/value
    triple; this is the pair that leaves out ``ast.AnnAssign``, because a rule
    about rebinding an existing name wants the writes carrying no annotation.
    """
    return tuple(
        node
        for node in _narrow(root, "unannotated-write", ast.Assign, ast.AugAssign)
        if isinstance(node, ast.Assign | ast.AugAssign)
    )


def async_function_defs(root: ast.AST) -> tuple[ast.AsyncFunctionDef, ...]:
    """Only the ``async def`` half of what `functions` returns together."""
    return tuple(
        node
        for node in _narrow(root, "async-function", ast.AsyncFunctionDef)
        if isinstance(node, ast.AsyncFunctionDef)
    )


def with_blocks(root: ast.AST) -> tuple[ast.With | ast.AsyncWith, ...]:
    """Every ``with``, in both its sync and async forms."""
    return tuple(
        node
        for node in _narrow(root, "with", ast.With, ast.AsyncWith)
        if isinstance(node, ast.With | ast.AsyncWith)
    )


def comprehensions(root: ast.AST) -> tuple[ast.ListComp | ast.SetComp | ast.GeneratorExp, ...]:
    """The comprehensions that do not build a dict."""
    return tuple(
        node
        for node in _narrow(root, "comprehension", ast.ListComp, ast.SetComp, ast.GeneratorExp)
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp)
    )


def assignments(nodes: Iterable[ast.AST]) -> Iterator[tuple[ast.AST, ast.expr, ast.expr | None]]:
    """Every ``(node, target, value)`` written by any of `nodes`, in walk order.

    "What does this node assign, and to what?" has exactly two answers --
    ``ast.Assign``'s ``targets``/``value`` and ``ast.AnnAssign``'s single
    ``target``/``value`` -- and detectors kept writing both as an ``isinstance``
    ladder over a local ``targets`` list they had to declare and re-initialise
    on every node. The ladder is the failure mode: a detector that writes the
    ``AnnAssign`` branch and forgets the ``Assign`` one under-reports silently,
    which is indistinguishable from a clean tree.

    The owning node rides along because a detector almost always reports against
    it, and recovering it afterwards means re-walking to line the two sequences
    up -- which is where a mismatched pair goes unnoticed.
    """
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield node, target, node.value
        elif isinstance(node, ast.AnnAssign):
            yield node, node.target, node.value


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
    """``app/crawl/admission.py`` -- the label a detector exempts a path by."""
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


def call_leaf_name(call: ast.Call) -> str:
    """The bare name a call is made through: ``uuid4()`` -> ``uuid4``, ``uuid.uuid4()`` -> ``uuid4``.

    ``getattr(call.func, "id", None) or getattr(call.func, "attr", None)`` is how a
    detector asks "is this a call to uuid4/HTTPException" without caring whether the
    module imported the name or reached it through a module. Spelled out it is one
    line, and it was written out separately by every detector that asked, which is
    how one of them ended up comparing against ``None``.
    """
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None) or ""


def bare_callee(node: ast.AST) -> str | None:
    """The bare function name ``node`` calls, or None when it is not such a call.

    ``int(x)``, ``uuid4()`` and ``_persist(...)`` are spelled
    ``ast.Call(func=ast.Name)``. A detector that wants only those has to test the
    shape itself, and detectors kept testing it themselves: the ``isinstance``
    pair this replaces was written out separately by each one, which is how the
    jscpd gate came to flag the same five lines in two files. Unlike
    :func:`call_leaf_name` this does not fall back to an attribute callee, so
    ``obj.int(x)`` is not a match.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def calls_named_leaf(tree: ast.AST, names: frozenset[str] | set[str]) -> Iterator[ast.Call]:
    """Yield calls whose leaf callee name is one of ``names``.

    :func:`calls_to_any` keys on the callee *expression* (a bare ``Name``), which
    is the right question for a helper a module imported directly and the wrong
    one for a chokepoint reached as ``module.name()``. Detectors for the second
    shape each re-derived the leaf name, so this is that derivation, once.
    """
    for call in call_nodes(tree):
        if call_leaf_name(call) in names:
            yield call


def attribute_calls(tree: ast.AST) -> Iterator[tuple[ast.Call, ast.Attribute]]:
    """Yield ``(call, callee)`` for every call whose callee is an attribute.

    The callee comes back narrowed alongside the call so a caller reads
    ``func.attr`` without re-asserting the type it just paid this walk to learn.
    """
    for node in nodes_of(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node, node.func


def single_target_name_assignments(
    nodes: Iterable[ast.AST],
    value_predicate: Callable[[ast.expr], bool],
) -> Iterator[tuple[ast.Assign, str]]:
    """Yield ``(node, name)`` for single-target Name assignments whose value matches.

    ``a, b = ...`` and ``obj.x = ...`` are deliberately not yielded: both callers
    are asking "which plain local names hold this kind of value", and a
    multi-target or attribute target is a different question.
    """
    for node in nodes:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and value_predicate(node.value):
            yield node, target.id


def local_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield ``node`` and its descendants, stopping at a nested definition.

    A detector asking "does THIS expression call strip()" must not be answered by
    a ``strip`` in a function defined inside it, so the walk does not descend
    into a nested ``def``/``async def``/``class`` body. Distinct from
    :func:`nodes_of`, which is whole-subtree and cached.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from local_nodes(child)


def local_call_leaf_names(tree: ast.AST, node: ast.AST) -> Iterator[tuple[str, ast.Call]]:
    """Yield ``(lowercased_leaf_name, call)`` for every call inside ``node``.

    ``resolved_call_name(tree, call).rsplit(".", 1)[-1].lower()`` is how a detector
    asks "is this a call to strip/lower/format/parse" without caring which module
    imported it. Spelled out it is four lines, and it was written out separately
    by every detector that asked.
    """
    for child in local_nodes(node):
        if isinstance(child, ast.Call):
            yield resolved_call_name(tree, child).rsplit(".", 1)[-1].lower(), child


def getattr_literal_name(call: ast.Call) -> str | None:
    """The attribute name of ``getattr(obj, "name")`` when it is a literal string.

    ``getattr(x, "strip")`` and ``x.strip()`` name the same method, and a detector
    that recognizes only the second is blind to the first. Returned lowercased so
    the caller compares against a lowercase name set.
    """
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        if isinstance(value, str):
            return value.lower()
    return None


def calls_naming_method(
    tree: ast.AST,
    node: ast.AST,
    method_names: frozenset[str],
) -> Iterator[ast.Call]:
    """Yield calls inside ``node`` that reach one of ``method_names``.

    Reaches a method two ways on purpose: a direct call (``s.strip()``) and a
    literal-named ``getattr`` (``getattr(s, "strip")()``). Aliasing a method
    through a variable is out of reach for any AST-only check and is not claimed
    to be.
    """
    for name, call in local_call_leaf_names(tree, node):
        if name in method_names:
            yield call
        elif name == "getattr" and getattr_literal_name(call) in method_names:
            yield call


def has_method_call(tree: ast.AST, node: ast.AST, method_names: frozenset[str]) -> bool:
    """Does ``node`` reach any of ``method_names``? The boolean form."""
    return next(calls_naming_method(tree, node, method_names), None) is not None


def spelled_non_member(value: ast.expr, members: Container[str]) -> str | None:
    """The string literal at ``value`` when it is not a member, else ``None``.

    The two decisions every "a spelled value must be a real member" detector has
    to make before it can report anything, and the two it gets wrong quietly: a
    non-literal is what a correct caller writes and passes without comment (so
    ``None`` is admitted without a special case -- the member set is a set of
    strings and the honest non-member is the one that is spelled), and a member
    is not a defect. Written per detector, the dropped ``isinstance`` guard does
    not fail a test -- it reports a variable as if it were a literal.
    """
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return None if value.value in members else value.value


def non_member_literals(
    candidates: Iterable[tuple[ast.Call, str, ast.expr, Container[str]]],
) -> Iterator[tuple[ast.Call, str, str]]:
    """``(node, field, value, members)`` tuples narrowed to spelled non-members.

    The set-valued funnel over :func:`spelled_non_member`, for the detectors that
    sweep more than one keyword at a time and so cannot fix ``members`` up front
    -- a Literal field's members are keyed by the field name, which is also what
    the offending message has to name. Reporting the value rather than the
    expression is deliberate: the message is about the value, and the only thing
    left for the caller to say is why it is wrong there.

    The node is an ``ast.Call`` because every caller feeds it the output of a
    call-keyword walk, which yields calls. Typing it as ``ast.AST`` widened the
    node on the way through and cost each caller the callee and the line it went
    on to read.
    """
    for node, field, value, members in candidates:
        spelled = spelled_non_member(value, members)
        if spelled is not None:
            yield node, field, spelled


def literal_keyword_membership_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    members,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
    label: str,
    type_name: str,
    advice: str,
) -> list[str]:
    """A literal passed to ``function_names(**keyword_names)`` that is not a member.

    The shared body of the "a spelled value must be a real enum member" detectors
    (result status, crawl strategy, ...). Each of those was the same seven lines
    over :func:`call_keyword_values` with a different message, and a hand-rolled
    copy is exactly where a member could be added to one axis and forgotten on
    the other. ``members`` is read from the live type at test time, never from a
    hand-kept exemption list.

    A non-string literal is never reported, which is what admits ``None`` for
    "no strategy was attempted" without a special case -- see
    :func:`spelled_non_member`, which owns that decision for every detector here
    and in ``tests/membership_gate.py`` rather than each restating it.
    """
    return [
        offender(
            path,
            lineno(node),
            f"{label}={value!r} is not a {type_name} member; {advice}",
        )
        for node, _field, value in non_member_literals(
            (node, str(kw.arg), kw.value, members)
            for node, kw in call_keyword_arguments(
                tree,
                function_names=function_names,
                keyword_names=keyword_names,
            )
        )
    ]


def assert_detector(
    detector,
    *,
    path: pathlib.Path,
    fires: dict[str, str] | None = None,
    trips: dict[str, int] | None = None,
    clean: tuple[str, ...] = (),
) -> None:
    """Prove a detector. Three orthogonal claims, one call, all exact.

    ``fires``  -- source -> the substring its single offender must name. The
    strongest claim: fires once, and says what it found.
    ``trips``  -- source -> how many offenders it must produce. For sources that
    legitimately contain the bad shape more than once (four hand-rolled cache
    envelopes; a ``uuid4`` call AND a ``.hex`` slice are two findings, not one).
    ``clean``  -- sources that must produce none.

    One call covering all of them because they fail in OPPOSITE directions and a
    bites test that can only easily express one is how a detector rots with the
    suite green: drop ``fires`` and nothing notices the detector has stopped
    detecting; drop ``clean`` and it has quietly grown over-broad. Every claim is
    exact, because ``assert offenders`` -- the hand-rolled form this replaced --
    proves only that a detector is not inert, and is equally satisfied by one that
    matches everything and by one whose message has degraded to a generic string.

    ``trips`` rejects 0 rather than accepting it: "must not fire" already has a
    spelling, and a count of 0 as a second one would let the same fact drift
    between two forms and make a mis-edited count read as a passing proof.

    Detector output is normalized to a list HERE rather than at each call site.
    The tree's detectors are split between ``list[str]`` and generators, and that
    split is worse than cosmetic: ``not <generator>`` is always True, so an
    over-matching generator detector would satisfy a hand-rolled ``assert not
    offenders`` while reporting everything. Normalizing once means a caller
    cannot get that wrong, and a new detector can be written either way.
    """
    for src, why in (fires or {}).items():
        offenders = list(detector(ast.parse(src), path))
        assert len(offenders) == 1, f"expected exactly 1 offender for {src!r}, got {offenders!r}"
        assert why in offenders[0], f"{offenders[0]!r} does not say {why!r}"
    for src, count in (trips or {}).items():
        if count < 1:
            raise ValueError(
                f"trips[{src!r}] = {count}: a source that must not fire belongs in clean, not trips"
            )
        offenders = list(detector(ast.parse(src), path))
        assert len(offenders) == count, f"expected {count} offenders for {src!r}, got {offenders!r}"
    for src in clean:
        offenders = list(detector(ast.parse(src), path))
        assert not offenders, f"fired on the clean shape {src!r}: {offenders!r}"


def assert_fires_once(detector, fires: dict[str, str], path: pathlib.Path) -> None:
    """A ``*_bites`` proof's red half: each source fires the detector, saying why.

    Thin wrapper over :func:`assert_detector` so the assertion exists in one
    place; kept as its own name because a number of proofs cover only the red
    half and reading as a wrapper is honest about that.
    """
    assert_detector(detector, path=path, fires=fires)


def assert_stays_clean(detector, clean: tuple[str, ...], path: pathlib.Path) -> None:
    """A ``*_bites`` proof's clean half: sanctioned shapes must not fire."""
    assert_detector(detector, path=path, fires={}, clean=clean)


#: The statements that open a nested scope -- the compound statements. This is
#: the vocabulary for "can contain a definition", and the metrics gate imports it
#: rather than restating it: two lists that happen to agree on these seven are
#: two lists that can stop agreeing.
SCOPE_OPENING_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)

#: A statement that owns a body -- a suite of statements. A walk asking "is this
#: the first statement of a block" is asking for exactly this, and ``ast.stmt``
#: -- the union every statement belongs to -- declares no ``body`` on any of
#: them, so the question could not be asked against it.
#:
#: Built as the scope-opening set plus the definition-bearing statements rather
#: than as a list that happens to agree with it.
BODY_OWNING_NODES: tuple[type[ast.AST], ...] = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
) + SCOPE_OPENING_NODES


def is_scope_opener(
    node: ast.AST,
) -> TypeGuard[ast.If | ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith | ast.Try]:
    """Whether ``node`` is a compound statement, narrowed to the kinds that are.

    The narrowed companion to :data:`SCOPE_OPENING_NODES`, for the walks that
    read ``node.body`` off the answer. Membership in the tuple and the narrowed
    type are one fact, and keeping them apart is how a walk ends up reading a
    member the declared type does not have.
    """
    return isinstance(node, SCOPE_OPENING_NODES)


def is_body_owner(
    node: ast.AST,
) -> TypeGuard[
    ast.Module
    | ast.FunctionDef
    | ast.AsyncFunctionDef
    | ast.ClassDef
    | ast.If
    | ast.For
    | ast.AsyncFor
    | ast.While
    | ast.With
    | ast.AsyncWith
    | ast.Try
]:
    """Whether ``node`` is a statement that owns a body, narrowed to the kinds that do.

    The predicate a walk needs rather than a bare ``isinstance``: the
    membership test and the narrowed type are the same fact, and separating them
    is how a check ends up written against a type that cannot answer it.
    """
    return isinstance(node, BODY_OWNING_NODES)


def calls_to_any(tree: ast.AST, function_names) -> Iterator[ast.Call]:
    """Yield direct calls whose callee is any of ``function_names``.

    The set-valued sibling of :func:`calls_named`. Several detectors track a set
    of names they *discovered* -- aliases, a builder propagated through
    assignments -- rather than one literal, and restating the
    ``isinstance(node.func, ast.Name) and node.func.id in names`` filter inline
    is how two detectors end up carrying the same scan shape twice.
    """
    for node in nodes_of(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in function_names:
            yield node


def named_calls_to_any(tree: ast.AST, function_names) -> Iterator[tuple[ast.Call, str]]:
    """:func:`calls_to_any`, paired with the callee's name.

    ``calls_to_any`` already filters on ``isinstance(node.func, ast.Name)``, so
    the name is always right there -- but ``Call.func`` is declared as the whole
    expression type, so a detector that wanted to *report* the callee had to
    re-assert that same narrowing, and three of them in the result-shape sweep
    were each carrying a copy. The assertion lives here, once, and a detector
    that reports a name it did not actually match is the failure this removes.
    """
    for call in calls_to_any(tree, function_names):
        assert isinstance(call.func, ast.Name), ast.unparse(call.func)
        yield call, call.func.id


def calls_named(tree: ast.AST, function_name: str) -> Iterator[ast.Call]:
    """Yield direct calls to one named function in an AST tree."""
    return calls_to_any(tree, {function_name})


def call_keyword_arguments(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str] | set[str],
    keyword_names: str | tuple[str, ...] | set[str] | None = None,
) -> Iterator[tuple[ast.Call, ast.keyword]]:
    """Yield ``(call, keyword)`` for selected keywords of direct calls to named functions.

    The one walk behind "what was spelled at these call sites". ``keyword_names``
    of ``None`` selects every named keyword (``**kwargs`` carries ``arg is None``
    and is never selectable -- it has no name to be a member of). Callers that
    only want the value use :func:`call_keyword_values`; callers that need the
    keyword's own name -- a Literal field's member set is keyed by it -- come
    here, because re-walking to recover the name is how two detectors end up
    matching a value against the wrong axis.
    """
    names = {function_names} if isinstance(function_names, str) else function_names
    keywords = (
        None
        if keyword_names is None
        else ({keyword_names} if isinstance(keyword_names, str) else set(keyword_names))
    )
    for node in calls_to_any(tree, names):
        # One pair per DISTINCT keyword name: Python itself rejects a repeated
        # keyword, so a call carrying two is un-compilable source, and emitting
        # an offender per occurrence would over-report against the one-per-call
        # behavior these detectors had before they were unified.
        seen_keywords: set[str | None] = set()
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in seen_keywords:
                continue
            if keywords is not None and keyword.arg not in keywords:
                continue
            seen_keywords.add(keyword.arg)
            yield node, keyword


def call_keyword_values(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
) -> Iterator[tuple[ast.Call, ast.expr]]:
    """Yield selected keyword values from direct calls to named functions."""
    for node, keyword in call_keyword_arguments(
        tree,
        function_names=function_names,
        keyword_names=keyword_names,
    ):
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
        if any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
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
            # Two candidate spellings, and the second is the fallback for an
            # empty first -- including for a call, which resolves to nothing.
            named = "" if isinstance(value, ast.Call) else _resolved_expr(value, bindings)
            canonical = named or _static_string(value, bindings)
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


def real_members(type_name: str) -> frozenset[str]:
    """The live member set of a taxonomy named by string.

    The one answer to "what is a real member of X", for the detectors that guard
    a call site against a literal that is not one. It exists because the
    detectors were taking the set as a keyword: every scan test re-derived it
    and every bites fixture could hand a detector a different answer to the
    question the detector exists to ask, which does not fail -- it approves the
    wrong literals.

    Two spellings, and the split is the load-bearing part rather than a
    convenience. A ``str``-Enum contributes its values, because a caller writes
    ``CrawlStrategy.HTTP_DECLARED_CRAWLER`` and the wire carries ``.value``. A
    ``Literal`` alias goes through ``app.models._literal_members`` rather than
    ``typing.get_args``, because that one is total over a union -- which is what
    lets a taxonomy be *defined* as a union of two others instead of restating
    one of them, and ``get_args`` would hand back the nested aliases instead of
    their members.

    Raises ``LookupError`` for a name that is not a taxonomy. A guard that
    silently resolved to the empty set would approve every literal, which is the
    failure this function exists to make impossible.
    """
    from app import api_models, constants, models

    for module in (models, api_models, constants):
        owner = getattr(module, type_name, None)
        if owner is not None:
            break
    else:
        raise LookupError(f"{type_name} is not a taxonomy on app.models/app.api_models/app.constants")
    if hasattr(owner, "__members__"):
        return frozenset(member.value for member in owner)
    return models._literal_members(owner)
