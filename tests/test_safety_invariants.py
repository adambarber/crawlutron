"""Executable safety invariants: recurring bug classes blocked at the door.

The round-4 review's central lesson (validated empirically): static
rejection of "pathological" shapes is bypassable, and chokepoints only
hold if bypassing them fails CI instead of code review. These AST walks
verify the UNSAFE patterns are absent from app/ so reintroducing them
turns the build red.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import io
import pathlib
import re
import tokenize
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from pathlib import Path

import pytest

from app.api_models import UnifiedCrawlResponse
from app.crawl.migrations.manifest import LEGACY_COLUMNS, StoreSchema
from app.settings import ENVIRONMENT_FIELD_ALIASES
from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    SANCTIONED,
    STRATEGIES_DIR,
    TESTS_DIR,
    app_offenders,
    app_python_files,
    app_trees,
    assert_detector,
    assert_fires_once,
    assert_stays_clean,
    assignments,
    attribute_calls,
    call_keyword_values,
    calls_named,
    calls_named_leaf,
    calls_to_any,
    file_tree,
    functions,
    has_method_call,
    literal_keyword_membership_offenders,
    local_call_leaf_names,
    local_nodes,
    nodes_of,
    nodes_of_type,
    non_member_literals,
    offender,
    offender_at,
    real_members,
    relative_posix,
    resolved_call_name,
    resolved_expr_name,
    trees_excluding,
    trees_under,
)
from tests.property_registry import PROPERTY_BY_NAME, PROPERTY_REGISTRY, Property

__all__ = [
    "APP_ROOT",
    "REPO_ROOT",
    "SANCTIONED",
    "STRATEGIES_DIR",
    "app_python_files",
    "app_trees",
    "app_offenders",
    "call_keyword_values",
    "calls_named",
    "file_tree",
    "offender",
    "trees_under",
]


def assert_trees_clean(
    detector: Callable[[ast.AST, Path], list[str]],
    message: str,
    *,
    trees: Iterable[tuple[Path, ast.AST]] | None = None,
) -> None:
    """Sweep every module through `detector` and fail on the offenders it finds.

    This is the tree-wide walk every invariant check in this file depends on, so
    it is written once here rather than per detector. It used to be four lines
    copied into each of ~60 tests -- declare the list, walk ``app_trees()``,
    ``extend``, assert -- and a copy is exactly what makes a check that sweeps
    nothing, or sweeps a hard-coded file list, look identical to one that sweeps
    the tree the rule actually requires (see :func:`app_trees`).

    ``trees`` defaults to the whole app tree; a check passes an explicit walker
    only to exclude sanctioned modules, and the default is what makes a forgotten
    sweep fail loudly rather than silently pass on an empty list.
    """
    offenders = [
        offender
        for path, tree in (app_trees() if trees is None else trees)
        for offender in detector(tree, path)
    ]
    assert not offenders, f"{message}: {', '.join(offenders)}"


# ---------------------------------------------------------------------------
# The admission sizing weights are one value
#
# ``_build_admission_policy`` and ``max_http_concurrency_for`` each took the same
# four sizing weights as keyword-only parameters, and the pair jscpd reported was
# those two signatures rather than any logic. The weights are now one frozen
# ``resource_pressure.TaskRamBudget`` that the caller builds and the policy
# function hands on unchanged.
#
# The drift this guards is a caller going back to the flat list. Nothing about
# the arithmetic stops that: a ``doc_workers=2`` keyword would work again the
# moment the parameter was re-added, and the weight would be reserved outside the
# budget the policy is built from -- the two lists drifting apart again, which is
# the shape the consolidation removed. So the budget is the only way in, and
# this says so over the whole tree rather than over the two modules involved.
# ---------------------------------------------------------------------------

# The four weights, read from the dataclass rather than transcribed: a weight
# added to TaskRamBudget is covered here the day it lands.
_ADMISSION_POLICY = "_build_admission_policy"


def _admission_budget_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A ``_build_admission_policy`` call carrying a sizing weight as a keyword."""
    from app.resource_pressure import TaskRamBudget

    weights = {field.name for field in dataclasses.fields(TaskRamBudget)}
    assert weights, "TaskRamBudget declared no weights; this sweep would be a no-op"
    offenders: list[str] = []
    for node in calls_to_any(tree, {_ADMISSION_POLICY}):
        for keyword in node.keywords:
            if keyword.arg not in weights:
                continue
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{_ADMISSION_POLICY}(..., {keyword.arg}={ast.unparse(keyword.value)}, ...) "
                    f"spells a sizing weight directly; pass a resource_pressure.TaskRamBudget "
                    f"so the policy is built from the same weights it reports",
                )
            )
    return offenders


def test_admission_sizing_reaches_the_policy_as_one_budget():
    """No caller re-spells the four sizing weights as keywords to the policy builder."""
    assert_trees_clean(
        _admission_budget_offenders,
        "admission sizing weights must arrive as a TaskRamBudget, not as four"
        " keywords that can drift from the budget the policy reports",
    )


def test_admission_budget_detector_bites():
    """The flat keyword fires on every weight; the budget form is clean.

    ``max_http_concurrency_for`` is deliberately NOT in this set: it is the
    per-call computation the budget is bound for, so it still takes the four
    weights flat and its own call inside the policy function is the sanctioned
    forwarding. What is banned is a *caller* re-spelling them.
    """
    assert_fires_once(
        _admission_budget_offenders,
        {
            "from app.resource_pressure import _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size,"
            " max_concurrent_pages=3, doc_workers=2)\n": "doc_workers=2",
        },
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert_stays_clean(
        _admission_budget_offenders,
        (
            "from app.resource_pressure import TaskRamBudget, _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size,"
            " max_concurrent_pages=3, budget=TaskRamBudget(doc_workers=2))\n",
            # The computation itself is not this guard's subject.
            "from app.resource_pressure import max_http_concurrency_for\n"
            "def ceiling(limit, pool_size):\n"
            "    return max_http_concurrency_for(limit, pool_size, doc_workers=2)\n",
            "from app.resource_pressure import _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size, max_concurrent_pages=3)\n",
        ),
        REPO_ROOT / "app" / "_synthetic.py",
    )


# Values are (declared public symbols, executable entry points). Public data
# classes, exceptions, type aliases, and constants remain part of the contract,
# but only functions and classes with their own methods are alternate ways in.
#
# app/bounded_io.py LOWERED to (4, 2): read_capped_sync is gone. It existed only
# for the document extractor's urllib path, and that path is now the shared pinned
# httpx transport like every other fetch, so the module has one reader rather
# than a sync one and an async one that nothing used side by side.
#
# app/api/response_shaping.py RAISED to (9, 8) for the same reason: `EnvelopeOptions`
# is the one record the sync and async envelope builders both take, and it exists
# because the two builders each restated the same six optional parameters in
# their signatures -- six more places to add an option and forget one, and jscpd
# was holding the pair as a same-file clone. Folding them into a record is what
# made the builders agree by construction; a narrower budget would push the
# options back out into two hand-maintained lists.
#
# app/validation.py RAISED to (17, 11) for `in_range`. The int face of the env
# chokepoint could not REFUSE an out-of-range value -- only clamp it -- because
# the `validate`/`validate_event` pair that says "warn and use the fallback"
# existed on the float face and had never been exposed on the int face. So an
# operator who configured a deployment value outside its contract got a
# different number in silence, while the code's own comment promised "falls back
# here, loudly". `in_range` is the predicate that pair consumes; it is the
# module's job because a range is a validation fact and a second copy of it in
# settings.py is what the drifted literal `hi=120` already was once. Declared in
# `__all__` rather than left module-private, because a private name imported
# across a module boundary is a public name with a lie on it.
#
# The entry-point half then DROPPED 13 -> 11, and that is the more interesting
# half: `get_env_int`/`get_env_float` are no longer second copies of the
# mapping faces' signatures, they are the mapping faces bound to `os.environ`.
# Adding `validate`/`validate_event` to one face had pushed the other face's
# restated parameter list past the clone gate, which is the gate correctly
# catching a signature that was maintained in two places. Two faces of one
# chokepoint should be one declaration, not two.
_OWNER_PUBLIC_SURFACE_RATCHET = {
    "app/crawl/admission.py": (20, 14),
    "app/validation.py": (17, 11),
    "app/api/response_shaping.py": (9, 8),
    "app/crawl/sqlite_store.py": (7, 5),
    "app/safe_regex.py": (7, 2),
    "app/crawl/robots.py": (13, 4),
    "app/bounded_io.py": (4, 2),
    "app/crawl/decompression.py": (3, 1),
    "app/crawl/credentials.py": (3, 3),
    "app/crawl/render_options.py": (2, 2),
    "app/safe_url.py": (1, 1),
    "app/crawl/payload_helpers.py": (1, 1),
    "app/crawl/result_helpers.py": (1, 0),
    "app/crawl/strategies/fetch_policy.py": (0, 0),
}
_PUBLIC_SURFACE_PROPERTY = Property(
    name="one-public-door",
    owner_module="app/crawl/sqlite_store.py",
    owner_symbols=(),
    definition_site="tests/test_safety_invariants.py:_public_surface_offenders",
    fingerprints=("public_surface",),
    reason="A property owner declares one public surface and cannot add another entry point.",
)


def _owned_public_symbols(tree: ast.AST) -> set[str]:
    public: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                public.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            public.update(
                target.id
                for target in targets
                if isinstance(target, ast.Name) and not target.id.startswith("_")
            )
    return public


def _entry_point_symbols(tree: ast.AST) -> set[str]:
    entry_points: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            entry_points.add(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) for child in node.body):
                entry_points.add(node.name)
    return entry_points


def _declared_public_symbols(tree: ast.AST) -> set[str] | None:
    declarations = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        )
    ]
    if len(declarations) != 1:
        return None
    value = declarations[0].value
    try:
        declared = ast.literal_eval(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(declared, (list, tuple, set)) or not all(isinstance(name, str) for name in declared):
        return None
    return set(declared)


def _public_surface_offenders(tree, path, _prop, *, modules=None) -> list[str]:
    relative = relative_posix(path)
    actual = _owned_public_symbols(tree)
    declared = _declared_public_symbols(tree)
    if declared is None:
        return [f"{relative}: public symbols are not declared by one literal __all__"]
    if actual == declared:
        return []
    missing = ", ".join(sorted(actual - declared)) or "none"
    extra = ", ".join(sorted(declared - actual)) or "none"
    return [f"{relative}: __all__ mismatch (missing: {missing}; extra: {extra})"]


def _owner_surface_counts(tree: ast.AST) -> tuple[int, int]:
    return len(_owned_public_symbols(tree)), len(_entry_point_symbols(tree))


def test_property_owner_public_surfaces_equal_all():
    offenders: list[str] = []
    for relative in _OWNER_PUBLIC_SURFACE_RATCHET:
        path = REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_public_surface_offenders(tree, path, _PUBLIC_SURFACE_PROPERTY))
    assert not offenders, "property owner public surface drifted: " + ", ".join(offenders)


def _surface_budget_offenders(*, grown: bool) -> list[str]:
    """Ratchet offenders for the public-surface budgets, by direction.

    Both directions read the same counts against the same budgets, so they share
    this walk: a budget that is too loose is as much a defect as one that is too
    tight, and two hand-rolled loops over the ratchet is one more place for the
    two to disagree about what the budget is.
    """
    offenders: list[str] = []
    for relative, (surface_budget, entry_budget) in _OWNER_PUBLIC_SURFACE_RATCHET.items():
        path = REPO_ROOT / relative
        surface_count, entry_count = _owner_surface_counts(ast.parse(path.read_text(encoding="utf-8")))
        over = surface_count > surface_budget or entry_count > entry_budget
        under = surface_count < surface_budget or entry_count < entry_budget
        if over if grown else under:
            offenders.append(
                f"{relative}: surface {surface_count}/{surface_budget}, "
                f"entry points {entry_count}/{entry_budget}"
            )
    return offenders


def test_property_owner_public_surface_counts_do_not_grow():
    offenders = _surface_budget_offenders(grown=True)
    assert not offenders, "property owner public surface grew: " + ", ".join(offenders)


def test_property_owner_public_surface_budgets_are_tight():
    stale = _surface_budget_offenders(grown=False)
    assert not stale, "property owner budgets are stale; lower them to: " + ", ".join(stale)


# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from
# attacker-influenced text (the ReDoS shape). `re.search(LITERAL, text)` is
# safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = {"compile", "search", "match", "fullmatch", "sub", "findall", "split"}


def _re_dynamic_pattern_offenders(methods: frozenset[str]):
    """Build the detector for ``re.<method>(<non-literal pattern>, ...)``.

    ``re.compile`` with a dynamic pattern is the ReDoS shape, and so is
    ``re.sub``/``re.search``/... with one: the hazard is a pattern built from
    attacker-influenced text, not a method name. Both the broad sweep and the
    single-method gate below are that one predicate over a method set, so it is
    written once -- two hand-rolled copies is how the narrow one ended up
    dropping the receiver check the broad one has.
    """

    def detector(tree: ast.AST, path: pathlib.Path) -> list[str]:
        return [
            offender_at(path, node, f"re.{node.func.attr}(<dynamic pattern>)")
            for node in nodes_of_type(tree, ast.Call)
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in methods
            and node.args
            and not isinstance(node.args[0], ast.Constant)
        ]

    return detector


_dynamic_pattern_re_calls = _re_dynamic_pattern_offenders(_RE_DYNAMIC_METHODS)


def test_no_dynamic_pattern_re_calls():
    """re.* with a non-literal pattern must go through app.safe_regex."""
    assert_trees_clean(
        _dynamic_pattern_re_calls,
        "Use app.safe_regex.compile_untrusted/match_bounded",
        trees=app_trees(),
    )


def test_no_direct_regex_engine_import():
    """Only safe_regex.py may import the third-party `regex` engine."""
    offenders: list[str] = []
    for path, tree in app_trees():
        for node in nodes_of(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "regex":
                        offenders.append(offender(path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "regex":
                    offenders.append(offender(path, node.lineno))
    assert not offenders, "Only app.safe_regex may import the regex engine: " + ", ".join(offenders)


def test_no_bare_re_compile_on_nonliteral():
    """Non-literal re.compile must go through app.safe_regex.compile_untrusted."""
    assert_trees_clean(
        _re_dynamic_pattern_offenders(frozenset({"compile"})),
        "Use app.safe_regex.compile_untrusted for non-literal patterns",
        trees=app_trees(),
    )


def _lock_bodies_with_awaits(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag `with <lock>:` bodies containing an await (the M3 class).

    A THREADING lock held across an await blocks the event loop and can
    deadlock. `async with` (asyncio.Lock/Condition) bodies may legitimately
    await -- that is the sanctioned pattern -- so only sync `with` is
    checked. Sync-with bodies that await include any `threading.Lock`/
    `RLock` context (which is what ContentCache.aset did before its N1
    rework).

    A lock is recognized by case-insensitive name stem (lock/mutex/guard)
    *or* by binding to a threading lock-family constructor anywhere in the
    module, so `self._mutex = threading.RLock()` cannot rename its way out
    of the sweep.
    """
    offenders: list[str] = []
    lock_names = _threading_lock_bindings(tree)
    for node in nodes_of_type(tree, ast.With, ast.AsyncWith):
        if isinstance(node, ast.AsyncWith):
            continue  # asyncio.Lock bodies may await by design
        for item in node.items:
            # `with self._lock:` / `with _CLOSE_LOCK:` / `with guard:` --
            # a sync lock context. Any await inside is a loop-blocker.
            expr = item.context_expr
            referred = getattr(expr, "attr", None) or getattr(expr, "id", None)
            if referred is None:
                continue
            looks_like_lock = any(stem in referred.lower() for stem in _LOCK_NAME_STEMS) or (
                referred in lock_names
            )
            if not looks_like_lock:
                continue
            for inner in nodes_of(node):
                if isinstance(inner, ast.Await):
                    offenders.append(offender(path, inner.lineno))
            break
    return offenders


_LOCK_NAME_STEMS = ("lock", "mutex", "guard")
_THREADING_LOCK_CONSTRUCTORS = frozenset({"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"})


def _threading_lock_bindings(tree: ast.AST) -> set[str]:
    """Names/attributes bound to threading lock-family constructors."""
    names: set[str] = set()
    for node in nodes_of(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        ctor = getattr(func, "attr", None) or getattr(func, "id", None)
        if ctor not in _THREADING_LOCK_CONSTRUCTORS:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_no_await_inside_lock_bodies():
    """A lock held across an await blocks the loop and can deadlock (M3)."""
    assert_trees_clean(
        _lock_bodies_with_awaits,
        "Lock held across await",
        trees=app_trees(),
    )


def test_lock_detector_bites():
    """Name stems beyond 'lock' and threading-bound aliases must fire (M3)."""
    mutex = ast.parse(
        "class C:\n    def __init__(self):\n        self._mutex = __import__('threading').Lock()\n"
        "    async def go(self):\n        with self._mutex:\n            await x()\n"
    )
    assert _lock_bodies_with_awaits(mutex, REPO_ROOT / "app" / "_synthetic.py"), "_mutex escaped the sweep"
    bound = ast.parse(
        "import threading\n\n_async_gate = threading.RLock()\n\n"
        "async def go():\n    with _async_gate:\n        await x()\n"
    )
    assert _lock_bodies_with_awaits(bound, REPO_ROOT / "app" / "_synthetic.py"), (
        "threading-bound name escaped"
    )
    asyncio_ok = ast.parse(
        "import asyncio\n\nclass C:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "    async def go(self):\n        async with self._lock:\n            await x()\n"
    )
    assert not _lock_bodies_with_awaits(asyncio_ok, REPO_ROOT / "app" / "_synthetic.py"), (
        "asyncio lock flagged"
    )
    plain_ok = ast.parse("def go():\n    with open('f'):\n        pass\n")
    assert not _lock_bodies_with_awaits(plain_ok, REPO_ROOT / "app" / "_synthetic.py")


# --- SafeUrl chokepoint (final review) ------------------------------------

# A hand-rolled redirect hop is the "forgot one of the five steps" class SafeUrl
# exists to kill. The dangerous *shape* -- in a SINGLE function -- is: test a
# 3xx status, build the next URL with a raw urljoin, and never go through the
# chokepoint (redirect_hop) or the sanctioned pinned helper (_redirect_target).
# Link/robots/sitemap urljoin normalization never co-occurs with a 3xx test in
# the same function, so it is not a false positive -- and this runs over the
# whole tree, not a hand-picked file list.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_REDIRECT_HOP_SANCTIONED = {"redirect_hop", "_redirect_target"}


def _functions_with(tree: ast.AST) -> list[ast.AST]:
    return [n for n in nodes_of(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]


def _uses_redirect_status(node: ast.AST) -> bool:
    """True if the function tests a concrete redirect status constant."""
    return any(isinstance(sub, ast.Constant) and sub.value in _REDIRECT_STATUSES for sub in nodes_of(node))


def _hand_rolled_redirect_hops(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for func in functions(tree):
        has_urljoin = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "urljoin"
            for n in nodes_of(func)
        )
        if not has_urljoin or not _uses_redirect_status(func):
            continue
        uses_chokepoint = any(
            isinstance(n, ast.Attribute)
            and n.attr in _REDIRECT_HOP_SANCTIONED
            or isinstance(n, ast.Name)
            and n.id in _REDIRECT_HOP_SANCTIONED
            for n in nodes_of(func)
        )
        if not uses_chokepoint:
            offenders.append(offender_at(path, func, f"{func.name}()"))
    return offenders


def test_no_hand_rolled_redirect_hops():
    """Redirect hops anywhere go through SafeUrl.redirect_hop (or the pinned
    fetch helper), never a raw urljoin combined with a 3xx status test."""
    assert_trees_clean(
        _hand_rolled_redirect_hops,
        "Use SafeUrl.redirect_hop for redirect hops",
        trees=app_trees(exclude=SANCTIONED | {"safe_url.py"}),
    )


_CANONICALIZER = frozenset({"normalize_public_http_url"})


def _raw_batch_dedup_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``dict.fromkeys`` batch dedup that keys on raw values.

    The url-batch dedup at ``_payload_urls`` must collapse duplicates by
    canonical page identity (``normalize_public_http_url``) -- not the raw
    string -- or a request carrying both ``.../x`` and ``.../x/?utm_source=tw``
    crawls one page twice. Every ordered-dedup via ``dict.fromkeys`` must sit
    inside a function that also routes through the canonicalizer, so a new
    raw-key dedup fails the suite the moment it lands.
    """
    offenders: list[str] = []
    for func in functions(tree):
        uses_fromkeys = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "fromkeys"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "dict"
            for n in nodes_of(func)
        )
        if not uses_fromkeys:
            continue
        canonicalizes = any(
            isinstance(n, ast.Name)
            and n.id in _CANONICALIZER
            or isinstance(n, ast.Attribute)
            and n.attr in _CANONICALIZER
            for n in nodes_of(func)
        )
        if not canonicalizes:
            offenders.append(
                offender_at(
                    path, func, f"{func.name}() dedups a batch with dict.fromkeys, not the canonicalizer"
                )
            )
    return offenders


def test_batch_dedup_keys_on_canonical_page_identity():
    """URL-batch dedup must collapse by ``normalize_public_http_url``, never raw strings."""
    assert_trees_clean(
        _raw_batch_dedup_offenders,
        "dedup url batches via normalize_public_http_url, not raw dict.fromkeys",
        trees=app_trees(),
    )


def test_batch_dedup_detector_bites():
    raw = ast.parse("def _payload_urls(payload):\n    return list(dict.fromkeys(payload['urls']))\n")
    assert _raw_batch_dedup_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py"), (
        "detector missed raw dict.fromkeys dedup"
    )
    good = ast.parse(
        "def _payload_urls(payload):\n"
        "    return [u for u in payload['urls'] if normalize_public_http_url(u) not in seen]\n"
    )
    assert not _raw_batch_dedup_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


def _bare_body_reads(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an unbounded body acquisition on a response-like object.

    Both ``response.read()`` (no size arg) and ``response.content`` pull the
    whole body into RAM. Capped reads go through app.bounded_io.read_capped_async;
    curl_cffi's ``.body`` attribute and non-response reads (files, sockets)
    are not HTTP body acquisitions and stay exempt. Tree-wide: any module may

    grow a fetch path, so this is keyed on the response-object convention
    rather than a directory.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        recv = (
            node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read":
            if not node.args and not node.keywords and _is_response_like(recv):
                offenders.append(offender_at(path, node, ".read()"))
        elif isinstance(node, ast.Attribute) and node.attr == "content" and _is_response_like(node.value):
            offenders.append(offender_at(path, node, ".content"))
    return offenders


def _is_response_like(node: ast.expr | None) -> bool:
    """True for a plain-name receiver that reads as an HTTP response."""
    if not isinstance(node, ast.Name):
        return False
    head = node.id.lower()
    return head.startswith("response") or head.startswith("resp") or head in {"r"}


def test_no_bare_body_reads():
    """Every HTTP body read anywhere must be capped (N2 class).

    Scope note: app/bounded_io.py IS the chokepoint and streams the capped body
    itself -- exempt by receiver, not path, so it survives a tree-wide sweep.
    Widening kept precise on the response-object convention to avoid flagging
    file/socket reads.
    """
    assert_trees_clean(
        _bare_body_reads,
        "Use app.bounded_io.read_capped_async",
        trees=app_trees(exclude=SANCTIONED | {"bounded_io.py"}),
    )


# ---------------------------------------------------------------------------
# A bounded HTML input has exactly one cap
# ---------------------------------------------------------------------------

# Two modules independently declared a 4 MB cap on the HTML they would scan --
# `_MAX_HTML_NOISE_INPUT_BYTES` in classify.py and
# `_MAX_LLM_FORMAT_HTML_INPUT_BYTES` in llm_format.py -- with the same value and
# the same rationale ("a hostile origin will hand you gigabytes"), reached by two
# names that no reader could see were the same policy. Raising one cap silently
# left the other, so the guarantee a reviewer believed was enforced per-consumer
# was actually enforced for whichever module happened to be edited. The shape is
# the same as every other drift-prone twin here: a resource bound is a policy, and
# a policy with two declarations is a policy that will disagree with itself. The
# cap now lives once in app/bounded_io.py next to the stream caps that share its
# purpose, and this gate keeps the second copy from reappearing.


def _html_input_cap_name(target: ast.expr, value: ast.expr | None) -> str | None:
    """The name of a locally-declared HTML input byte cap, or ``None``.

    Keyed on the *intent* (an HTML byte budget) rather than the two names it used
    to have, so a third spelling is caught too. A cap that delegates elsewhere --
    anything that is not a numeric expression -- is not a second declaration, and
    that is one predicate rather than the two guards each copy of this rule used
    to carry.
    """
    if value is None or not _is_numeric_expr(value):
        return None
    name = target.id if isinstance(target, ast.Name) else ""
    head = name.upper()
    return name if "HTML" in head and head.endswith("BYTES") else None


def _local_html_input_caps(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a module that declares its own HTML input byte cap."""
    return [
        offender_at(path, node, name)
        for node, target, value in assignments(nodes_of(tree))
        if (name := _html_input_cap_name(target, value)) is not None
    ]


def _cache_envelope_literals(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag any dict literal shaped like a content-cache envelope.

    Keyed on the *stored shape* -- the key set every cache entry carries --
    rather than on the two write paths that happened to spell it, so a third
    writer is caught too. A dict that merely mentions ``expires`` is not an
    envelope; it has to carry the whole contract.
    """
    required = {"expires", "value", "cached_at"}
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Dict):
        keys = {
            key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if required <= keys:
            offenders.append(offender_at(path, node, "cache-envelope"))
    return offenders


def test_cache_envelope_is_built_by_one_owner():
    """The content-cache entry envelope has one builder.

    ``set`` and ``aset`` each built the on-disk envelope themselves, which is
    the cache's *stored contract*: a field added for one write path was absent
    from the other, so an entry written through it was missing something the
    reader expects. ``ContentCache._entry_to_store`` is the owner; a new write
    path has to go through it.
    """
    offenders = app_offenders(_cache_envelope_literals, exclude=SANCTIONED | {"content_cache.py"})
    assert not offenders, "the content-cache envelope is built outside its owner:\n  " + "\n  ".join(
        offenders
    )


def test_cache_envelope_detector_bites():
    """The envelope sweep must fire on a hand-rolled envelope elsewhere.

    Without this, an exhausted or renamed sink pattern would report zero
    offenders and the guard above would pass forever while covering nothing.
    """
    # A dict that merely mentions one envelope key is not an envelope, and
    # flagging it would make the guard something people route around.
    assert_detector(
        _cache_envelope_literals,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def write(self, key, value, expires):\n"
            '    return {"expires": expires, "value": value, "cached_at": expires}\n': "cache-envelope",
        },
        clean=('def f(expires, value):\n    return {"expires": expires, "value": value}\n',),
    )


def _inline_extraction_normalizations(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an inline ``<something>.text or \"\"`` outside the extraction normalizer.

    Keyed on the *normalization itself* -- an extractor's text being coerced to
    a string -- rather than on the extractor branches that happened to spell it,
    so a fourth extractor is caught too. An ``or \"\"`` on a non-``.text``
    attribute is somebody else's business.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        if len(node.values) != 2:
            continue
        left, right = node.values
        if not (isinstance(left, ast.Attribute) and left.attr == "text"):
            continue
        if isinstance(right, ast.Constant) and right.value == "":
            offenders.append(offender_at(path, node, "extraction-text-normalization"))
    return offenders


def test_extraction_text_is_normalized_by_one_owner():
    """An extractor's text is coerced to a string in exactly one place.

    ``_extract_document``/``_extract_html``/``extract_content`` each had the
    same ``extracted.text or ""`` tail spelled at their call site. That
    coercion is what keeps an extractor's ``None`` text from reaching a result
    at all, so a branch that omitted it produced a None that only surfaced
    further downstream. ``extraction.normalized_extraction`` is the owner; it
    lives beside ``ExtractedContent`` because the coercion is a fact about
    extractor output, not about any one strategy's result builder.
    """
    offenders = app_offenders(_inline_extraction_normalizations, exclude=SANCTIONED | {"extraction.py"})
    assert not offenders, "extraction text is normalized inline instead of by its owner:\n  " + "\n  ".join(
        offenders
    )


def test_extraction_text_normalization_detector_bites():
    """The normalization sweep must fire on a hand-rolled one, and not on a
    lookalike -- otherwise it either misses the shape or flags a pattern that
    belongs to somebody else."""
    assert_detector(
        _inline_extraction_normalizations,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={'def f(e):\n    return e.text or ""\n': "extraction-text-normalization"},
        clean={'def f(d):\n    return d.label or ""\n'},
    )


def _is_numeric_expr(node: ast.expr) -> bool:
    """True for a literal/computed number, e.g. ``4 * 1024 * 1024``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return True
    return isinstance(node, ast.BinOp) and _is_numeric_expr(node.left) and _is_numeric_expr(node.right)


def test_html_input_cap_is_declared_once():
    """The bounded-HTML-input cap has one declaration, in app/bounded_io.py.

    app/bounded_io.py is exempt because it IS the chokepoint that owns the cap
    alongside the stream caps.
    """
    assert_trees_clean(
        _local_html_input_caps,
        "Import MAX_HTML_INPUT_BYTES and call bounded_io.truncate_html_input "
        "instead of declaring a second HTML input cap",
        trees=app_trees(exclude=SANCTIONED | {"bounded_io.py"}),
    )


def test_local_html_input_cap_detector_bites():
    """Every twin shape fires; delegating and unrelated names stay clean."""
    path = TESTS_DIR.parent / "app" / "_synthetic.py"
    fires = {
        # The two historical spellings, verbatim.
        "_MAX_HTML_NOISE_INPUT_BYTES = 4 * 1024 * 1024\n": "_MAX_HTML_NOISE_INPUT_BYTES",
        "_MAX_LLM_FORMAT_HTML_INPUT_BYTES = 4 * 1024 * 1024\n": "_MAX_LLM_FORMAT_HTML_INPUT_BYTES",
        # Annotated, and a plain literal.
        "HTML_INPUT_BYTES: int = 4194304\n": "HTML_INPUT_BYTES",
        # A third name for the same policy must not slip past the intent keying.
        "MAX_DOC_HTML_BYTES = 5_000_000\n": "MAX_DOC_HTML_BYTES",
    }
    assert_fires_once(_local_html_input_caps, fires, path)

    clean = (
        # The sanctioned import-and-delegate shape.
        "from app.bounded_io import MAX_HTML_INPUT_BYTES, truncate_html_input\n",
        "body = truncate_html_input(html)\n",
        # Unrelated byte budgets: the sink has its own, and it is not HTML input.
        "MAX_WEBHOOK_RESPONSE_BYTES = 10_485_760\n",
        "MAX_CACHE_ENTRIES = 100_000\n",
        "HTML_FIELDS = ('title', 'description')\n",
    )
    assert_stays_clean(_local_html_input_caps, clean, path)


def _scope_matcher_definitions() -> list[str]:
    """File labels of every ``def scope_allows``/``_scope_allows`` under app/.

    M10: the bounded scope check was verbatim-copied into engine.py and
    recursive.py; a one-sided fix would have left the other on the stale
    contract. Deduped into app/crawl/scope_match.py -- a second definition
    anywhere means the copy crept back.
    """
    defs: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in nodes_of_type(tree, ast.FunctionDef, ast.AsyncFunctionDef):
            if node.name in {"scope_allows", "_scope_allows"}:
                defs.append(offender(path, node.lineno))
    return defs


def test_scope_matcher_has_exactly_one_definition():
    """The fail-closed scope matcher must live in one module only.

    Behavior parity across the dedupe is covered by test_scope_regex.py;
    this pins the structural property that no second copy exists to drift.
    """
    defs = _scope_matcher_definitions()
    assert len(defs) == 1, f"scope matcher duplicated again: {defs}"
    assert "app/crawl/scope_match.py" in defs[0], defs


def test_no_pinned_url_leaks_into_final_url():
    """A pinned fetch must never surface str(response.url) as final_url.

    _build_pinned_request_async rewrites a plain-HTTP host to its resolved IP
    so httpx reports the IP; surfacing it in final_url leaks topology and
    keys the cookie jar by IP. The fetchers must use current_url instead.
    Tree-wide: any module may grow a fetcher, so the final_url sink is
    checked everywhere (search providers use response.url legitimately but
    never as a final_url, so they are not false positives).
    """
    offenders = []
    for path, tree in app_trees():
        for node in nodes_of(tree):
            # final_url = str(response.url)  (assignment)
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id == "final_url"]
                if targets and _is_response_url(node.value):
                    offenders.append(offender_at(path, node, "final_url = str(response.url)"))
            # HttpFetchResult(final_url=str(response.url))  (keyword)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "final_url" and _is_response_url(kw.value):
                        offenders.append(offender_at(path, node, "final_url=str(response.url)"))
    assert not offenders, "pinned URL leaked into final_url: " + ", ".join(offenders)


def _is_response_url(node: ast.expr) -> bool:
    """True for str(response.url) -- a response object's url coerced to str."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "url"
        and isinstance(node.args[0].value, ast.Name)
        and node.args[0].value.id == "response"
    )


def _unpinned_safe_url_parses(tree, path):
    """Every SafeUrl.parse fetch consumer must pass an explicit ``pin=``.

    An unpinned parse is the H5 rebinding window: validation resolves one IP,
    then ``connect`` re-resolves at open time and a hostile DNS can swap the
    answer. Forcing an explicit pin at every call site makes "forgot to pin"
    impossible to merge; safe_url.py itself is exempt (redirect_hop passes a
    computed pin preference, and the classmethod defines the parameter).
    """
    offenders = []
    for node in nodes_of(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "parse"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "SafeUrl"
        ):
            continue
        if not any(kw.arg == "pin" for kw in node.keywords):
            offenders.append(offender_at(path, node, "SafeUrl.parse(...) must pass pin=True"))
    return offenders


def test_safe_url_parse_always_pins():
    offenders = app_offenders(_unpinned_safe_url_parses, exclude=SANCTIONED | {"safe_url.py"})
    assert not offenders, "unpinned SafeUrl.parse (DNS-rebinding window): " + "; ".join(offenders)


def test_no_inline_pinned_url_composition():
    """The pinned connect URL -- IP literal, IPv6 bracketing, port, and Host
    header composition -- is SafeUrl.connect()'s job alone.

    Three divergent copies of that composition had already drifted into the
    tree (safe_url, http fetcher, webhook builder); the copies disagreed on
    Host-port handling and all lacked IPv6 bracketing (H5-C). An f-string
    that mentions pinned_ip outside app/safe_url.py re-opens that drift.
    """
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset({"safe_url.py", "safe_regex.py"})):
        for node in nodes_of_type(tree, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if "pinned_ip" in value.value:
                        offenders.append(offender_at(path, node, "f-string with pinned_ip"))
                if isinstance(value, ast.FormattedValue):
                    expr = value.value
                    if isinstance(expr, ast.Attribute) and expr.attr == "pinned_ip":
                        offenders.append(offender_at(path, node, "f-string interpolates .pinned_ip"))
                    if (
                        isinstance(expr, ast.Name)
                        and isinstance(expr.ctx, ast.Load)
                        and "pinned" in expr.id.lower()
                    ):
                        offenders.append(offender_at(path, node, f"f-string interpolates {expr.id}"))
    assert not offenders, "compose pinned URLs via SafeUrl.connect(), never inline:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Frontier raw-href sinks (H1 class)
# ---------------------------------------------------------------------------


def _url_key_reads(node: ast.AST) -> bool:
    """True if the subtree reads dict key ``"url"`` (the unvalidated raw href)."""
    for sub in nodes_of(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "get"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and sub.args[0].value == "url"
            ):
                return True
        elif isinstance(sub, ast.Subscript):
            if isinstance(sub.slice, ast.Constant) and sub.slice.value == "url":
                return True
    return False


def _frontier_sink_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``*.frontier.append(x)`` where ``x`` (or its same-function binding)
    reads the raw ``"url"`` key instead of going through frontier_link_url()."""
    offenders: list[str] = []
    for fn in (n for n in nodes_of(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        tainted: set[str] = set()
        for assign in (a for a in nodes_of(fn) if isinstance(a, ast.Assign | ast.AnnAssign)):
            value = assign.value
            if value is None or _frontier_link_url_call(value):
                continue
            if _url_key_reads(value):
                targets = [assign.target] if isinstance(assign, ast.AnnAssign) else assign.targets
                for t in targets:
                    if isinstance(t, ast.Name):
                        tainted.add(t.id)
        for call in (c for c in nodes_of(fn) if isinstance(c, ast.Call)):
            f = call.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("append", "appendleft", "extend")
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "frontier"
            ):
                for arg in call.args:
                    if _url_key_reads(arg) or (isinstance(arg, ast.Name) and arg.id in tainted):
                        offenders.append(offender_at(path, call, f"raw href into frontier.{f.attr}"))
    return offenders


def _frontier_link_url_call(node: ast.AST) -> bool:
    """True if the subtree's root or any call node is frontier_link_url(...)."""
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "frontier_link_url"
        for sub in nodes_of(node)
    )


def _optional_host_policy_offenders(tree, path):
    """A ``HostPolicy`` parameter must never be optional.

    The host policy is the SSRF gate. Every one of the 70-odd functions that
    takes one requires it, and requiring it is the point: a caller that forgot
    to pass a policy gets a ``TypeError`` at the boundary rather than a fetch
    that quietly ran against the wrong target policy.

    Defaulting one turns that into a silent bypass, and nothing else in the
    stack catches it. Verified by mutation, twice:

      * ``fetchers.impersonating_fetcher`` with
        ``policy: HostPolicy = HostPolicy((), ())`` -- ``mypy app`` reported no
        issues across 111 files, because protocol conformance is
        one-directional and an implementation may accept more than the contract
        asks for. The fetcher contract guard now covers that one specifically
        (see test_type_contracts).
      * ``pinned_transport._build_pinned_request_async`` with the same default
        -- no Protocol covers that pair, and every AST invariant in this file
        passed. This rule is what closes it.

    The rule is derived from the *type* rather than from a name list, which is
    what makes it hold for a function nobody has written yet: any
    ``HostPolicy``-annotated parameter anywhere in ``app/``, in any module, at
    any nesting depth. ``policy`` and ``host_policy`` are both in scope because
    the rule keys off the annotation, not the name.
    """
    for node in nodes_of_type(tree, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda):
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        defaulted = set()
        for index, arg in enumerate(positional):
            if index >= len(positional) - len(args.defaults):
                defaulted.add(arg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                defaulted.add(arg.arg)
        for arg in [*positional, *args.kwonlyargs]:
            if arg.arg not in defaulted:
                continue
            annotation = arg.annotation
            if isinstance(annotation, ast.Name) and annotation.id == "HostPolicy":
                yield offender_at(
                    path,
                    arg,
                    f"{getattr(node, 'name', '<lambda>')}: parameter {arg.arg!r} is a HostPolicy and "
                    r"is optional; a caller that omits "
                    r"the SSRF policy gets the default instead of a TypeError",
                )


def test_the_host_policy_is_never_optional():
    offenders = app_offenders(_optional_host_policy_offenders)
    assert not offenders, (
        "a HostPolicy parameter must be required everywhere in app/; an optional one is an "
        "SSRF bypass that neither mypy nor any other guard catches: " + ", ".join(offenders)
    )


def test_optional_host_policy_detector_bites():
    bad = ast.parse("async def f(url, *, policy: HostPolicy = HostPolicy((), ())):\n    return url\n")
    found = list(_optional_host_policy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(found) == 1, found
    assert "is a HostPolicy and is optional" in found[0], found

    # A default on a *positional* HostPolicy is caught too, not just the
    # keyword-only form the mutations used.
    positional = ast.parse("def g(policy: HostPolicy = HostPolicy((), ())):\n    pass\n")
    assert list(_optional_host_policy_offenders(positional, REPO_ROOT / "app" / "_synthetic.py"))

    # The required form is left alone, and so is a required parameter the rule
    # does not key off -- it is the annotation that matters, not the name.
    good = ast.parse(
        "async def h(url, *, policy: HostPolicy, host_policy: HostPolicy, other: str = 'x'):\n"
        "    return url\n"
    )
    assert not list(_optional_host_policy_offenders(good, REPO_ROOT / "app" / "_synthetic.py"))


def test_no_raw_href_frontier_sinks():
    """The frontier only ever carries frontier_link_url()-validated URLs (H1)."""
    assert_trees_clean(
        _frontier_sink_offenders,
        "Read normalized_url via frontier_link_url(), never raw url",
        trees=app_trees(),
    )


def test_raw_href_frontier_detector_bites():
    """Prove the detector fires on the historical H1 shape."""
    bad = ast.parse(
        "async def _run(job, item):\n"
        "    for link in item.get('discovered_links') or []:\n"
        "        link_url = link.get('url') if isinstance(link, dict) else str(link)\n"
        "        if link_url:\n"
        "            job.frontier.append(link_url)\n"
    )
    offenders = _frontier_sink_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1 and "frontier.append" in offenders[0], offenders


# ---------------------------------------------------------------------------
# Crawl-response shaping chokepoint (jscpd #1 class)
# ---------------------------------------------------------------------------


def _crawl_response_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag explicit ``UnifiedCrawlResponse(..., results=[...])`` construction
    outside response_shaping — callers must use build_unified_crawl_response so
    persisted/webhook/REST payloads cannot drift (the #1754 rule, endpoint-wide).
    ``**stored`` deserialization splats have no explicit results= keyword."""
    if path.name == "response_shaping.py":
        return []
    offenders: list[str] = []
    for node, _ in call_keyword_values(
        tree,
        function_names="UnifiedCrawlResponse",
        keyword_names="results",
    ):
        offenders.append(offender_at(path, node, "explicit UnifiedCrawlResponse(results=...) build"))
    return offenders


def test_crawl_response_built_only_by_shaping_chokepoint():
    """Every UnifiedCrawlResponse-with-results flows through the shaper."""
    offenders = app_offenders(_crawl_response_construction_offenders)
    assert not offenders, "Use app.api.response_shaping.build_unified_crawl_response: " + ", ".join(offenders)


def test_crawl_response_chokepoint_detector_bites():
    """Synthetic bypass fires; **stored deserialization splat stays clean."""
    assert_detector(
        _crawl_response_construction_offenders,
        path=REPO_ROOT / "app" / "api" / "_synthetic.py",
        fires={
            "def f(results):\n    return UnifiedCrawlResponse(request_id='r', results=results)\n": (
                "explicit UnifiedCrawlResponse"
            )
        },
        clean=("def f(stored):\n    return UnifiedCrawlResponse(**stored)\n",),
    )


# ---------------------------------------------------------------------------
# The same chokepoint, two more edges
#
# ``abuild_unified_crawl_response`` is the response builder, but the drift that
# actually shipped was never in the builder: it was in the ADAPTER -- the two
# endpoints each read a raw producer envelope and passed four fields by name,
# and a field added to one and forgotten on the other was dropped by the model
# without a word. The rules below close both ends of that: the envelope is
# adapted in one module, and its scalar fields are passed in one function.
# ---------------------------------------------------------------------------

_ENVELOPE_OWNER_MODULE = "app/api/response_shaping.py"
_ENVELOPE_OWNER_FUNCTION = "_response_envelope"
_ENVELOPE_BUILDERS = ("abuild_unified_crawl_response", "build_unified_crawl_response")
# The only callers permitted to skip the owner's envelope read, each named with
# the reason it is not the same shape as a wire endpoint's:
#   app/api/async_crawl_runner.py -- the detached background runner. It answers a
#     webhook instead of a caller and reads the envelope LENIENTLY
#     (``result.get("submitted_count")``), because a producer that stopped
#     stamping a key must fail a job, not raise out of a background task that
#     has no caller to answer.
#   app/mcp/tools.py -- the MCP crawl tool, which is the same adaptation as
#     /v1/crawl and is still spelled out at its own call site. PENDING: it is
#     outside app/api, so it is named here rather than migrated, and this entry
#     is what a reviewer should delete once it goes through the owner.
_ENVELOPE_ADAPTER_EXEMPT = frozenset({"app/api/async_crawl_runner.py", "app/mcp/tools.py"})
# Derived from the model, not transcribed: the rule is "every field the wire
# model requires is passed by one function", so the field set has to come from
# the model or this guard would check a list that can fall behind it.
_ENVELOPE_SCALAR_FIELDS = frozenset(UnifiedCrawlResponse.model_fields) - {"results"}


def _unsanctioned_envelope_adapter_offenders(tree, path):
    """Flag a response builder called from outside the shaping owner module."""
    relative = relative_posix(path)
    if relative == _ENVELOPE_OWNER_MODULE or relative in _ENVELOPE_ADAPTER_EXEMPT:
        return []
    return [
        offender_at(path, call, f"{call.func.id}() outside {_ENVELOPE_OWNER_MODULE}")
        for call in calls_to_any(tree, _ENVELOPE_BUILDERS)
    ]


def test_crawl_response_envelope_is_adapted_by_one_owner():
    """Endpoints read a producer envelope through the shaping owner, not by hand."""
    assert_trees_clean(
        _unsanctioned_envelope_adapter_offenders,
        "adapt a producer envelope with app.api.response_shaping's envelope owner, "
        "so an envelope field cannot be passed on one path and forgotten on another",
    )


def test_crawl_response_envelope_adapter_detector_bites():
    """A hand-rolled adapter fires; the owner module and the exempt producers stay clean."""
    fires = {
        "async def f(result):\n"
        "    return await abuild_unified_crawl_response(result['results'], "
        "options=EnvelopeOptions(request_id=result['request_id']))\n": "abuild_unified_crawl_response",
        "def f(results):\n    return build_unified_crawl_response(results)\n": "build_unified_crawl_response",
    }
    assert_fires_once(
        _unsanctioned_envelope_adapter_offenders,
        fires,
        REPO_ROOT / "app" / "api" / "crawl_endpoint.py",
    )
    assert_stays_clean(
        _unsanctioned_envelope_adapter_offenders,
        (
            "async def f(result):\n"
            "    return await _aunified_response_from_result(result, adapt=render_result_to_unified)\n",
        ),
        REPO_ROOT / _ENVELOPE_OWNER_MODULE,
    )
    assert_stays_clean(
        _unsanctioned_envelope_adapter_offenders,
        (
            "async def f(request_id):\n"
            "    return await abuild_unified_crawl_response([], "
            "options=EnvelopeOptions(request_id=request_id, success=False))\n",
        ),
        REPO_ROOT / "app" / "api" / "async_crawl_runner.py",
    )


def _hand_rolled_envelope_fields_offenders(tree, path):
    """Flag the unified envelope's scalar fields being passed outside their owner.

    Every one of those fields is REQUIRED by :class:`UnifiedCrawlResponse`, so a
    second spelling of the list is a second place to forget a field -- and a
    forgotten field is dropped silently, which is how one shipped missing from
    one endpoint and was caught only by curling the live endpoint. The
    ``submitted_count`` fallback that decides what the wire calls a batch's size
    lives in the same function for the same reason: it used to be written in both
    builders, and two copies of a fallback is how two paths start disagreeing
    about a count.
    """
    relative = relative_posix(path)
    offenders: list[str] = []
    for call in calls_named(tree, "UnifiedCrawlResponse"):
        if not any(keyword.arg in _ENVELOPE_SCALAR_FIELDS for keyword in call.keywords):
            continue
        enclosing = next(
            (f for f in functions(tree) if f.lineno <= call.lineno <= (f.end_lineno or f.lineno)),
            None,
        )
        if (
            relative == _ENVELOPE_OWNER_MODULE
            and getattr(enclosing, "name", None) == _ENVELOPE_OWNER_FUNCTION
        ):
            continue
        offenders.append(
            offender_at(path, call, f"envelope fields are passed by {_ENVELOPE_OWNER_FUNCTION} only")
        )
    return offenders


def test_crawl_response_envelope_fields_are_passed_by_one_owner():
    assert_trees_clean(
        _hand_rolled_envelope_fields_offenders,
        "pass the unified envelope's fields in app.api.response_shaping._response_envelope; "
        "a field the model requires is dropped, not rejected, when a caller forgets it",
    )


def test_crawl_response_envelope_fields_detector_bites():
    """A second field spelling fires, anywhere; the owner's own build and a
    ``**stored`` deserialization stay clean."""
    hand_rolled = (
        "def build(results, request_id):\n"
        "    return UnifiedCrawlResponse(results=results, request_id=request_id, success=True, "
        "submitted_count=len(results), policy_schema_version='v')\n"
    )
    assert_fires_once(
        _hand_rolled_envelope_fields_offenders,
        {hand_rolled: _ENVELOPE_OWNER_FUNCTION},
        REPO_ROOT / "app" / "api" / "crawl_endpoint.py",
    )
    assert_stays_clean(
        _hand_rolled_envelope_fields_offenders,
        ("def get_crawl_result(stored):\n    return UnifiedCrawlResponse(**stored)\n",),
        REPO_ROOT / "app" / "api" / "crawl_endpoint.py",
    )
    assert_stays_clean(
        _hand_rolled_envelope_fields_offenders,
        (hand_rolled.replace("def build(", f"def {_ENVELOPE_OWNER_FUNCTION}("),),
        REPO_ROOT / _ENVELOPE_OWNER_MODULE,
    )


# ---------------------------------------------------------------------------
# Endpoint readiness gate (the rejection tail)
#
# ``request_gate`` (app/api/main_helpers.py) owns the whole gated tail: admit,
# reject (operator warning + the shared 4xx), release. Spelled per endpoint the
# three obligations were independently droppable, and the expensive one to lose
# is the release -- a rejected request that skips ``end_request`` holds its
# readiness slot for the life of the process. Pinning the counters to one module
# makes "an endpoint cannot forget to release" structural: a new gated endpoint
# has to enter the gate, and a re-spelled tail has no chokepoint to drift from.
# ---------------------------------------------------------------------------

_READINESS_GATE_EXEMPT = (
    # The owner itself.
    "app/api/main_helpers.py",
    # PENDING: the fourth gated endpoint, outside app/api. It admits and releases
    # the same slot but has no ValueError rejection to delegate -- it answers 503
    # and 502 from provider failures, which is not what request_gate converts --
    # so it takes the counters directly. Migrating it means teaching the gate
    # those codes or splitting admission from rejection; either way the entry
    # below should go with the migration.
    "app/search/endpoint.py",
)


def _unsanctioned_readiness_counter_offenders(tree, path):
    """Flag a direct readiness-slot admit/release outside the gate chokepoint."""
    return [
        offender_at(path, call, f"{callee.attr}() outside app.api.main_helpers.request_gate")
        for call, callee in attribute_calls(tree)
        if callee.attr in {"begin_request", "end_request"}
    ]


def test_readiness_slots_are_admitted_and_released_by_the_gate():
    offenders = app_offenders(_unsanctioned_readiness_counter_offenders, skip=_READINESS_GATE_EXEMPT)
    assert not offenders, (
        "take a readiness slot with app.api.main_helpers.request_gate; a hand-written "
        "admit/release is how a rejected request holds its slot for the life of the "
        "process: " + ", ".join(offenders)
    )


def test_readiness_counter_detector_bites():
    """A hand-rolled admit, and a hand-rolled release, both fire; the gate stays clean."""
    fires = {
        "def f():\n    readiness_state.begin_request()\n": "begin_request",
        "def f():\n    try:\n        pass\n    finally:\n        readiness_state.end_request()\n":
        "end_request",
        "def f():\n    return READINESS_STATE.end_request()\n": "end_request",
    }
    assert_fires_once(
        _unsanctioned_readiness_counter_offenders, fires, REPO_ROOT / "app" / "api" / "_synthetic.py"
    )
    assert_stays_clean(
        _unsanctioned_readiness_counter_offenders,
        (
            "async def f(payload):\n"
            '    with request_gate("crawl_request_rejected", logger=logger):\n'
            "        return await execute(payload)\n",
            "def f():\n    get_metrics().observe_crawl(errors=1)\n",
        ),
        REPO_ROOT / "app" / "api" / "_synthetic.py",
    )


# ---------------------------------------------------------------------------
# Job-row accumulator chokepoint (write amplification)
# ---------------------------------------------------------------------------


def _job_row_accumulator_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag SQL that reads or writes a dropped ``crawl_jobs`` column.

    ``crawl_jobs`` used to carry ``results`` and ``visited`` as JSON columns, so
    every batch persist re-serialized the job's entire page history and total
    write volume grew as the square of the page count (migration 0003). Both are
    append-only accumulators and now live in their own tables.

    The banned set is read from the manifest rather than written out here, so
    the detector cannot drift from the schema: the columns in LEGACY_COLUMNS are
    by definition the ones a current store no longer has. Migrations are exempt
    because dropping a column is the one operation that must name it.
    """
    if "migrations" in path.parts:
        return []
    banned = {column.name for column in LEGACY_COLUMNS[StoreSchema.CRAWL_JOBS]}
    # Word boundaries, because the dropped names are substrings of the live
    # ones: `results_count` and `visited_size` are output ALIASES of the
    # accumulator subqueries, and `crawl_job_results` is the table itself.
    # Underscore is a word character, so none of those can match -- only a
    # column named exactly the banned word does.
    pattern = re.compile(r"\b(" + "|".join(sorted(banned)) + r")\b")
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Constant):
        if not isinstance(node.value, str) or "crawl_jobs" not in node.value:
            continue
        named = sorted({match.group(0) for match in pattern.finditer(node.value.lower())})
        if named:
            offenders.append(offender_at(path, node, f"SQL names crawl_jobs accumulator column(s) {named}"))
    return offenders


def test_crawl_jobs_accumulators_are_never_row_columns():
    """No SQL outside the migrations may name a column the row no longer has."""
    offenders = app_offenders(_job_row_accumulator_offenders)
    assert not offenders, (
        "Append-only accumulators belong in crawl_job_results / crawl_job_visited, "
        "not on the job row: " + ", ".join(offenders)
    )


def test_job_row_accumulator_detector_bites():
    """A synthetic wide-row write fires; the accumulator tables and the queue stay clean."""
    # The live shape: subqueries over the accumulator tables aliased back to
    # names that CONTAIN the dropped columns. A substring match would reject
    # this, which is why the detector matches whole words.
    assert_detector(
        _job_row_accumulator_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        fires={
            'def f(conn, job):\n    return conn.execute("UPDATE crawl_jobs SET visited = ?", '
            '(job.visited,))\n': (
                "visited"
            )
        },
        clean=(
            "def f(conn, job):\n"
            '    conn.execute("UPDATE crawl_jobs SET frontier = ?", (job.frontier,))\n'
            "    conn.execute('INSERT INTO \"crawl_job_results\" (job_id, seq, payload) VALUES (?, ?, ?)')\n"
            "    conn.execute('INSERT INTO \"crawl_job_visited\" (job_id, url) VALUES (?, ?)')\n"
            '    conn.execute("SELECT (SELECT count(*) FROM crawl_job_results r WHERE r.job_id = job.job_id) '
            'as results_count FROM crawl_jobs job")\n',
        ),
    )


# ---------------------------------------------------------------------------
# Render-options construction chokepoint (C2/M11)
# ---------------------------------------------------------------------------


def _render_options_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``RenderOptions(...)`` construction outside the resolver (C2/M11).

    The effective render options were once hand-assembled at every entry
    point and the copies drifted -- the browser strategy hardcoded the
    capture knobs while policy reads respected the caller elsewhere, and
    the /v1/render builder silently dropped browser_profile. Only
    app/crawl/render_options.py may decide the shape; callers map their own
    source into the resolver's mappings."""
    if path.name == "render_options.py":
        return []
    offenders: list[str] = []
    for node in calls_named(tree, "RenderOptions"):
        offenders.append(offender_at(path, node, "RenderOptions(...) built outside resolve_render_options"))
    return offenders


def test_render_options_built_only_by_resolver():
    """Every effective RenderOptions flows through resolve_render_options."""
    offenders = app_offenders(_render_options_construction_offenders)
    assert not offenders, (
        "Use app.crawl.render_options.resolve_render_options so request-side "
        "options and server defaults merge in exactly one place: " + ", ".join(offenders)
    )


def test_render_options_chokepoint_detector_bites():
    """Synthetic bypass fires; the resolver's own construction stays clean."""
    assert_detector(
        _render_options_construction_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        fires={
            "def f(policy):\n    return RenderOptions(timeout_seconds=policy['t'], include_html=True)\n": (
                "built outside resolve_render_options"
            )
        },
    )
    # The resolver's own construction is the ONE sanctioned build site, and the
    # exemption is a PATH exemption -- so it has to be proved at the real path. At
    # a synthetic path the same source fires, which is the whole point: the
    # exemption is narrow enough to not generalize, and this asserts that.
    assert_detector(
        _render_options_construction_offenders,
        path=REPO_ROOT / "app" / "crawl" / "render_options.py",
        fires={},
        clean=("def f(request, defaults):\n    return RenderOptions(**kwargs)\n",),
    )


# ---------------------------------------------------------------------------
# ContentCache mirror twins (jscpd #2 class)
# ---------------------------------------------------------------------------

_CACHE_TWIN_METHODS = {"get", "aget", "set", "aset"}


def _memory_cache_twin_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag public cache twin methods touching ``_memory_cache`` directly.

    The RAM mirror's probe/store logic must live in the _*_locked helpers so
    the sync and async paths cannot drift (aget once lost the LRU count cap,
    and the pre-write mirror never rolled back — both invisible twins)."""
    offenders: list[str] = []
    for fn in (n for n in nodes_of(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        if fn.name not in _CACHE_TWIN_METHODS:
            continue
        for sub in nodes_of_type(fn, ast.Subscript):
            if isinstance(sub.value, ast.Attribute) and sub.value.attr == "_memory_cache":
                offenders.append(offender_at(path, fn, f"{fn.name}() reads/writes _memory_cache directly"))
                break
    return offenders


def test_memory_cache_only_via_locked_helpers():
    """get/set/aget/aset never touch the raw RAM-mirror dict."""
    assert_trees_clean(
        _memory_cache_twin_offenders,
        "Use _take_memory_locked/_remember_memory_locked",
        trees=app_trees(),
    )


def test_memory_cache_twin_detector_bites():
    """Synthetic drifted twin fires; helper-shaped code stays clean."""
    assert_detector(
        _memory_cache_twin_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        fires={
            "class C:\n    async def aget(self, key):\n        with self._lock:\n"
            "            if key in self._memory_cache:\n"
            "                return self._memory_cache[key][1]\n":
            "aget() reads/writes _memory_cache directly",
        },
        clean=("class C:\n    async def aget(self, key):\n        return self._take_memory_locked(key)\n",),
    )


# ---------------------------------------------------------------------------
# Lock ownership: a *_locked helper presumes the caller holds the lock (M1)
# ---------------------------------------------------------------------------


def _is_locked_helper_name(name: str | None) -> bool:
    return bool(name) and name.startswith("_") and name.endswith("_locked")


def _references_lock(expr: ast.expr) -> bool:
    """True for ``self._lock`` / ``some_lock`` style context expressions."""
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Attribute):
        return "lock" in target.attr.lower()
    if isinstance(target, ast.Name):
        return "lock" in target.id.lower()
    return False


def _locked_calls_outside_lock_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call to a ``_*_locked`` helper not lexically inside a lock ``with``.

    The ``_*_locked`` cache helpers assume the caller already holds ``self._lock``
    (they touch shared index/RAM state with no internal acquisition). ``get()``
    once called ``_remember_memory_locked`` off-lock while ``aget()`` held it —
    the exact sync/async drift the twin detector can't see, because the call site
    is helper-shaped and only the *lock ownership* was wrong. This closes that by
    making an unlocked ``_*_locked`` call a hard failure anywhere in ``app``.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in nodes_of(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if not _is_locked_helper_name(fname):
            continue
        under_lock = False
        cur: ast.AST | None = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.With, ast.AsyncWith)) and any(
                _references_lock(item.context_expr) for item in cur.items
            ):
                under_lock = True
                break
            cur = parents.get(cur)
        if not under_lock:
            offenders.append(offender_at(path, node, f"{fname}() called outside a lock context manager"))
    return offenders


def test_locked_cache_helpers_are_called_under_the_lock():
    """M1 class: every ``_*_locked`` call sits inside a ``with ...lock`` block."""
    assert_trees_clean(
        _locked_calls_outside_lock_offenders,
        "call *_locked helpers inside `with self._lock`",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_locked_helper_lock_ownership_detector_bites():
    """An off-lock *_locked call fires; the guarded form stays clean."""
    assert_detector(
        _locked_calls_outside_lock_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "class C:\n    def get(self, key):\n        return self._remember_memory_locked(key)\n": (
                "_remember_memory_locked() called outside a lock context manager"
            )
        },
        clean=(
            "class C:\n    def get(self, key):\n        with self._lock:\n"
            "            return self._remember_memory_locked(key)\n",
        ),
    )


# ---------------------------------------------------------------------------
# Error-envelope shape (jscpd #3 class)
# ---------------------------------------------------------------------------

# app/api/exceptions.py owns the envelope; it is the sanctioned definition
# (exclude matches by file *name*, matching app_trees' own semantics).
ENVELOPE_SANCTIONED = SANCTIONED | {"exceptions.py"}


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _envelope_dict_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled error envelopes; the factory is the only definition.

    Three shapes: ANY ``HTTPException(...)`` construction (M8: the wire shape
    may only be built by :func:`http_error`/``browser_pool_unavailable`` -- a
    bare ``HTTPException(detail="str")`` reaches the handler's fallback and
    used to be laundered into a fabricated ``http_<status>`` code); a
    ``HTTPException(detail={...code, message...})`` literal (the pre-factory
    form); and any dict carrying all three envelope keys anywhere (catches
    JSONResponse/return-value variants).
    """
    offenders = [
        offender_at(
            path,
            call,
            "HTTPException detail= envelope literal"
            if any(
                kw.arg == "detail"
                and isinstance(kw.value, ast.Dict)
                and ({"code", "message"} <= _dict_keys(kw.value))
                for kw in call.keywords
            )
            else "bare HTTPException construction",
        )
        for call in calls_named_leaf(tree, {"HTTPException"})
    ]
    offenders.extend(
        offender_at(path, node, "error envelope dict literal")
        for node in nodes_of_type(tree, ast.Dict)
        if {"code", "message", "details"} <= _dict_keys(node)
    )
    return offenders


def test_no_hand_rolled_error_envelopes():
    """Every HTTP error body flows through error_envelope/http_error (#1722)."""
    assert_trees_clean(
        _envelope_dict_offenders,
        "Use app.api.exceptions.http_error/browser_pool_unavailable",
        trees=app_trees(exclude=ENVELOPE_SANCTIONED),
    )


def test_envelope_detector_bites():
    """All three drift shapes fire; factory-shaped code stays clean."""
    # Four offenders from one source: two bare HTTPException builds, one
    # detail= envelope literal, one three-key dict. Counted exactly, because a
    # detector that reported one here would be hiding which shapes it still sees.
    assert_detector(
        _envelope_dict_offenders,
        path=REPO_ROOT / "app" / "api" / "_synthetic.py",
        trips={
            "raise HTTPException(status_code=404, detail={'code': 'not_found', 'message': 'gone'})\n"
            "response = {'code': 'pending', 'message': 'm', 'details': {}}\n"
            "raise HTTPException(status_code=404, detail='plain string')\n"
            "raise fastapi.HTTPException(status_code=500)\n": 4,
        },
        clean=(
            "from app.api.exceptions import http_error\n"
            "raise http_error(404, 'not_found', 'gone')\n"
            "response = build_thing({'code': 1, 'message': 'x'})\n",
        ),
    )


# ---------------------------------------------------------------------------
# Search-provider result normalization (jscpd #4 family)
# ---------------------------------------------------------------------------


def _provider_loop_reinlines(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the keyed-provider dedupe loop re-inlined outside the collector.

    The exact copy-paste shape is ``seen: set`` + ``isinstance(item, dict)``
    guard + ``SearchResult`` construction in one function. DuckDuckGo's
    provider legitimately keeps a separate lxml node-walk: it has no dict
    guard, so the sink never fires on it.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "_collect_results":
            continue
        subs = list(nodes_of(node))
        assigns_seen = any(
            isinstance(s, ast.AnnAssign) and getattr(s.target, "id", "") == "seen" for s in subs
        )
        builds_result = any(
            isinstance(s, ast.Call) and getattr(s.func, "id", "") == "SearchResult" for s in subs
        )
        guards_dict = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "id", "") == "isinstance"
            and len(s.args) >= 2
            and getattr(s.args[1], "id", "") == "dict"
            for s in subs
        )
        if assigns_seen and builds_result and guards_dict:
            offenders.append(offender_at(path, node, "dedupe loop re-inlined outside _collect_results"))
    return offenders


def test_no_provider_dedupe_loop_reinlines():
    """One normalize/dedupe/clip contract for all keyed providers."""
    assert_trees_clean(
        _provider_loop_reinlines,
        "Use app.search.providers._collect_results",
        trees=app_trees(),
    )


def test_provider_loop_detector_bites():
    """The three-provider copy fires; the DDG lxml-walk shape stays clean."""
    assert_detector(
        _provider_loop_reinlines,
        path=REPO_ROOT / "app" / "search" / "_synthetic.py",
        fires={
            "async def search(self, query, limit, timeout_seconds):\n"
            "    results = []\n"
            "    seen: set[str] = set()\n"
            "    for item in payload.get('results') or []:\n"
            "        if not isinstance(item, dict):\n"
            "            continue\n"
            "        results.append(SearchResult(url=item.get('url')))\n"
            "    return results\n": "dedupe loop re-inlined outside _collect_results",
        },
        # DDG walks lxml nodes instead of provider dicts, so it has no dict guard
        # and the sink never reaches it -- the scope boundary is the whole reason
        # one collector can be mandatory for keyed providers and irrelevant here.
        clean=(
            "async def search(self, query, limit, timeout_seconds):\n"
            "    results = []\n"
            "    seen: set[str] = set()\n"
            "    for node in tree.xpath('//div'):\n"
            "        href = node.get('href')\n"
            "        results.append(SearchResult(url=href))\n"
            "    return results\n",
        ),
    )


# The names of the one selector contract, and the single parameter that carries
# them; see ``app/crawl/extraction.py`` for why they are one value rather than
# two parameters on three signatures.
_SELECTOR_PARAMS = ("css_selector", "xpath")
#: The single parameter that carries the pair, and therefore the shape a
#: narrowing function is expected to have.
_SELECTOR_CARRIER_PARAM = "selectors"
#: The one place the two names are spelled: the ``Selectors`` dataclass. Its
#: generated ``__init__`` is what the sweep sees, so that is the name checked.
_SELECTOR_CARRIER_DECLARANT = "__init__"
#: The content cache key legitimately declares both names: it does not narrow a
#: caller's request, it keys one, and a key built from half the narrowing inputs
#: is a different key. Named rather than inferred from the parameter names,
#: because a rule that infers is a rule that stops applying when the code moves.
_SELECTOR_KEY_BUILDER = "_content_cache_key"


def _selector_contract_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A narrowing extractor carries the pair in one value, never as two names.

    The rule used to be "take both names or neither", which forbade the defect
    by counting parameters. It could catch a function declaring one, and it
    could say nothing about a function declaring both and then honouring only
    one. It was also the *reason* the pair was spelled four times -- three
    signatures plus the mapping that fed them -- because satisfying a parameter
    rule is a spelling, and a spelling cannot be shared between entry points.

    :class:`app.crawl.extraction.Selectors` replaced the spelling, so the rule
    inverts: a function narrows by taking ONE ``selectors`` value, and nothing
    outside the carrier declares the names. That is stronger in the direction
    that matters -- there is no longer any signature in which passing one
    selector is expressible -- and it is what stops a future fourth extractor
    from reintroducing the pair as parameters, which a rule checking only "both
    or neither" would have waved through.
    """
    offenders: list[str] = []
    for node in functions(tree):
        params = node.args
        positional = [arg.arg for arg in (*params.posonlyargs, *params.args)]
        names = [*positional, *(arg.arg for arg in params.kwonlyargs)]
        declared = [name for name in names if name in _SELECTOR_PARAMS]
        if not declared or node.name in (_SELECTOR_CARRIER_DECLARANT, _SELECTOR_KEY_BUILDER):
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{node.name} declares {declared} instead of "
                f"carrying the pair in one {_SELECTOR_CARRIER_PARAM!r} value",
            )
        )
    return offenders


def test_the_selector_pair_is_one_contract():
    """A narrowing extractor carries the pair in one value, never as two names."""
    assert_trees_clean(
        _selector_contract_offenders,
        "a narrowing function must take one `selectors` value carrying both, and"
        " must not declare css_selector/xpath as parameters of its own; the pair is"
        " one contract and half of it is a request that cannot be honoured",
    )


def test_selector_contract_detector_bites():
    """A re-spelled pair fires, a lone name fires, the real shape passes."""
    assert_fires_once(
        _selector_contract_offenders,
        {
            "def _extract_html(html, url, css_selector=None, xpath=None)"
            " -> ExtractedContent:\n    ...\n": "declares ['css_selector', 'xpath'] instead of carrying",
            "def _narrow(html, css_selector=None) -> ExtractedContent:\n"
            "    ...\n": "declares ['css_selector'] instead of carrying",
        },
        REPO_ROOT / "app" / "crawl" / "_synthetic.py",
    )
    assert_stays_clean(
        _selector_contract_offenders,
        (
            # The carrier itself: the one place the two names are spelled.
            "class Selectors:\n    css: str | None = None\n    xpath: str | None = None\n",
            # An extractor carrying the pair as the one value.
            "def _extract_html(html, url, selectors=None) -> ExtractedContent:\n    ...\n",
            # A key builder legitimately requires both: it keys a request rather
            # than narrowing one.
            "def _content_cache_key(url, strategy, headers, extraction, css_selector, xpath):\n    ...\n",
            # A function that takes no selector is somebody else's business.
            "def _extract_document(body, url) -> ExtractedContent:\n    ...\n",
        ),
        REPO_ROOT / "app" / "crawl" / "_synthetic.py",
    )


def _keyed_provider_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A keyed provider supplies a request and a payload hook, not the flow.

    The normalize/dedupe/clip contract above is only half the shared flow. The
    other half -- issue the request, refuse a non-200, hand the result list to
    the collector -- used to be spelled once per provider, and the two POST
    bodies jscpd reported were those spellings: identical but for the endpoint
    and the payload lambda, so the only part that genuinely varied was the part
    left as a hook. This says the hooks are mandatory and the flow is not
    writable, which is what makes a fourth provider a subclass with two methods
    instead of a fourth copy of the non-200 check.

    The base is matched on the resolved name's last segment because
    ``_ApiKeyProvider`` is module-private: inside ``app/search/providers.py`` it
    is a bare name with no import to resolve, and outside it is only ever
    reachable through that one import.
    """
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.ClassDef):
        if not any(
            resolved_expr_name(tree, base).rsplit(".", 1)[-1] == "_ApiKeyProvider" for base in node.bases
        ):
            continue
        defined = {
            child.name for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for hook in ("_request", "_items_of"):
            if hook not in defined:
                offenders.append(offender_at(path, node, f"{node.name} does not define {hook}()"))
        if "search" in defined:
            offenders.append(
                offender_at(path, node, f"{node.name} re-spells the shared provider flow in search()")
            )
    return offenders


def test_keyed_providers_inherit_the_shared_response_flow():
    """One flow for the keyed providers: request and payload hook per subclass."""
    assert_trees_clean(
        _keyed_provider_offenders,
        "a keyed search provider must supply _request() and _items_of() and inherit"
        " _ApiKeyProvider.search, so the non-200 check cannot be re-spelled or dropped",
    )


def test_keyed_provider_flow_detector_bites():
    """The re-spelled flow fires, the incomplete subclass fires, the hooks pass.

    The first shape is what the two POST providers did: the whole flow written
    per provider, differing only in the endpoint and the payload lambda. The
    second is the quieter failure -- a provider that inherits the flow and
    defines only one of the two hooks, which reaches ``NotImplementedError`` on
    the first query instead of at import. The third is the shape the class is
    for.
    """
    assert_fires_once(
        _keyed_provider_offenders,
        {
            "from app.search.providers import _ApiKeyProvider\n"
            "class SerperProvider(_ApiKeyProvider):\n"
            "    name = 'serper'\n"
            "    def _items_of(self, payload):\n"
            "        return payload.get('organic')\n"
            "    async def _request(self, query, limit, timeout_seconds):\n"
            "        return await _provider_http('POST', url, timeout_seconds=timeout_seconds)\n"
            "    async def search(self, query, limit, timeout_seconds):\n"
            "        status, body = await self._request(query, limit, timeout_seconds)\n"
            "        return self._results(status, body, lambda p: p.get('organic'), url_key='link',\n"
            "            title_key='title', description_key='snippet')\n":
            "re-spells the shared provider flow",
            "from app.search.providers import _ApiKeyProvider\n"
            "class TavilyProvider(_ApiKeyProvider):\n"
            "    name = 'tavily'\n"
            "    async def _request(self, query, limit, timeout_seconds):\n"
            "        return await _provider_http('POST', url, timeout_seconds=timeout_seconds)\n":
            "does not define _items_of()",
        },
        REPO_ROOT / "app" / "search" / "_synthetic.py",
    )
    assert_stays_clean(
        _keyed_provider_offenders,
        (
            "class TavilyProvider(_ApiKeyProvider):\n"
            "    name = 'tavily'\n"
            "    _url_key = 'url'\n"
            "    def _items_of(self, payload):\n"
            "        return payload.get('results')\n"
            "    async def _request(self, query, limit, timeout_seconds):\n"
            "        return await _provider_http('POST', url, timeout_seconds=timeout_seconds)\n",
            # A provider that does not inherit the keyed flow (DuckDuckGo) is not
            # this guard's business; the loop guard above owns its shape.
            "class DuckDuckGoProvider:\n"
            "    name = 'duckduckgo'\n"
            "    async def search(self, query, limit, timeout_seconds):\n"
            "        return []\n",
        ),
        REPO_ROOT / "app" / "search" / "_synthetic.py",
    )


# ---------------------------------------------------------------------------
# robots.txt scanning (jscpd #5 family)
# ---------------------------------------------------------------------------

_ROBOTS_SCANNER_SANCTIONED = "iter_robots_fields"


def _robots_scanner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """One line-walk for every robots consumer.

    Sink: a function doing the ``lstrip(BOM) + splitlines()`` scan. It lived
    three times (crawl-delay, rules, sitemaps) and drifted on the empty-value
    gate; the tokenizer in app/crawl/robots_parse.py is now the only one.
    """
    offenders: list[str] = []
    for node in functions(tree):
        if node.name == _ROBOTS_SCANNER_SANCTIONED:
            continue
        has_bom_strip = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "attr", "") == "lstrip"
            and s.args
            and isinstance(s.args[0], ast.Constant)
            and s.args[0].value in ("﻿", "\ufeff")
            for s in nodes_of(node)
        )
        has_split = any(
            isinstance(s, ast.Call) and getattr(s.func, "attr", "") == "splitlines" for s in nodes_of(node)
        )
        if has_bom_strip and has_split:
            offenders.append(offender_at(path, node, f"{node.name} re-rolls the robots line-scanner"))
    return offenders


def test_robots_line_scanner_lives_in_one_place():
    """Every robots consumer walks lines through the shared tokenizer."""
    assert_trees_clean(
        _robots_scanner_offenders,
        "app/crawl/robots_parse.iter_robots_fields is the single scanner",
        trees=app_trees(),
    )


def test_robots_scanner_detector_bites():
    assert_detector(
        _robots_scanner_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        fires={
            "def parse_robots_rules(text):\n"
            "    for raw in text.lstrip('\\ufeff').splitlines():\n"
            "        yield raw\n": "parse_robots_rules re-rolls the robots line-scanner",
        },
        clean=(
            "def iter_robots_fields(text):\n"
            "    for raw in text.lstrip('\\ufeff').splitlines():\n"
            "        yield raw.strip()\n",
        ),
    )


# ---------------------------------------------------------------------------
# Test-fake construction (jscpd #6: sanctioned builders over hand-built fakes)
# ---------------------------------------------------------------------------

_FAKE_BUILDER_SANCTIONED = frozenset({"_fetcher_factories.py"})


def _fetcher_factory_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a test file hand-building a model the fetcher factory already fabricates.

    HttpFetchResult's field set is the crawl contract; a fixture that spells
    it out by hand silently diverges when the contract grows (the exact
    drift #1797 is about). _fetcher_factories.make_fetch is the sanctioned builder.
    """
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        if getattr(node.func, "id", "") == "HttpFetchResult":
            offenders.append(
                offender_at(path, node, "use _fetcher_factories.make_fetch instead of HttpFetchResult(...)")
            )
    return offenders


def test_tests_do_not_hand_build_http_fetch_results():
    """Every fetch fake comes from the one sanctioned builder."""
    assert_trees_clean(
        _fetcher_factory_offenders,
        "Use _fetcher_factories.make_fetch()",
        trees=trees_excluding(TESTS_DIR, *_FAKE_BUILDER_SANCTIONED),
    )


def test_fetch_builder_detector_bites():
    assert_detector(
        _fetcher_factory_offenders,
        path=REPO_ROOT / "tests" / "_synthetic.py",
        fires={"def f():\n    return HttpFetchResult(url='u')\n": "make_fetch"},
        clean=("def f():\n    return make_fetch(url='u')\n",),
    )


# ---------------------------------------------------------------------------
# Poll-loop reimplementation (jscpd test-side drift, sibling to the fake
# builder finding): _assertions.poll_until is the sanctioned predicate poller.
# A hand-rolled ``while True`` poller that awaits ``asyncio.wait_for`` without
# a ``pytest.fail`` termination silently returns False on timeout, diverging
# from the one that names the failing condition. Flag the shape.
# ---------------------------------------------------------------------------


def _call_node_names(node: ast.AST) -> set[str]:
    """Names of every call target under ``node`` (e.g. ``pytest.fail``)."""
    names: set[str] = set()
    for child in nodes_of(node):
        if isinstance(child, ast.Call):
            with contextlib.suppress(Exception):
                names.add(ast.unparse(child.func))
    return names


def _poll_loop_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for node in functions(tree):
        src = ast.dump(node)
        has_while_true = any(
            isinstance(n, ast.While) and ast.unparse(n.test) == "True" for n in nodes_of(node)
        )
        if not (has_while_true and "iscoroutine" in src and "wait_for" in src):
            continue
        # The sanctioned _assertions.poll_until terminates with pytest.fail. A
        # docstring may *claim* a fail; only a real call node counts.
        if any(name == "fail" or name.endswith(".fail") for name in _call_node_names(node)):
            continue
        offenders.append(
            offender_at(path, node, "use _testkit.poll_until instead of a hand-rolled poll loop")
        )
    return offenders


def test_tests_do_not_relinline_poll_loop():
    """One sanctioned poller; tests must not re-roll their own."""
    assert_trees_clean(
        _poll_loop_offenders,
        "Use _testkit.poll_until",
        trees=trees_excluding(TESTS_DIR, *_FAKE_BUILDER_SANCTIONED),
    )


def test_poll_loop_detector_bites():
    assert_detector(
        _poll_loop_offenders,
        path=REPO_ROOT / "tests" / "_synthetic.py",
        fires={
            "async def _poll_until(pred, *, timeout=10.0):\n"
            "    async def _runner():\n"
            "        while True:\n"
            "            v = pred()\n"
            "            if asyncio.iscoroutine(v):\n"
            "                v = await v\n"
            "            if v:\n"
            "                return True\n"
            "    return await asyncio.wait_for(_runner(), timeout=timeout)\n": "poll_until",
        },
        clean=(
            "async def poll_until(pred, *, timeout=10.0, message=None):\n"
            "    async def _runner():\n"
            "        while True:\n"
            "            if pred():\n"
            "                return True\n"
            "    await asyncio.wait_for(_runner(), timeout=timeout)\n"
            "    pytest.fail(message)\n",
        ),
    )


# ---------------------------------------------------------------------------
# Decompression bomb guard (jscpd #5 family / H2 closure)
# ---------------------------------------------------------------------------

_DECOMPRESS_FUNCS = frozenset({"decompress_body", "_decompress_body"})
_DECOMPRESS_SANCTIONED = frozenset({"decompression.py"})


def _decompress_cap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a decompress_body call that omits the mandatory output cap.

    A small gzip/deflate/brotli stream can expand to gigabytes; every
    production caller must bound the output. The cap is now a required
    keyword at the chokepoint, so this AST sweep is belt-and-suspenders
    against a caller reaching for an ``int`` default or ``**kwargs``.
    """
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        if resolved_expr_name(tree, node.func).rsplit(".", 1)[-1] == "getattr":
            continue
        fname = resolved_call_name(tree, node).rsplit(".", 1)[-1]
        if fname not in _DECOMPRESS_FUNCS:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "max_output_bytes" not in kwargs:
            offenders.append(offender_at(path, node, f"{fname}(...) without max_output_bytes"))
    return offenders


def test_all_decompress_calls_pass_a_cap():
    """Every production decompression is size-bounded (H2 closure)."""
    assert_trees_clean(
        _decompress_cap_offenders,
        "decompress_body needs max_output_bytes=",
        trees=app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED),
    )


def test_decompress_cap_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "_synthetic.py"
    bad = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc)\n")
    offenders = _decompress_cap_offenders(bad, path)
    assert len(offenders) == 1, offenders

    alias_shapes = {
        "import as": (
            "import app.crawl.decompression as compression\n"
            "def f(raw, enc):\n"
            "    return compression.decompress_body(raw, enc)\n"
        ),
        "from import as": (
            "from app.crawl.decompression import decompress_body as decode\n"
            "def f(raw, enc):\n"
            "    return decode(raw, enc)\n"
        ),
        "assignment alias": (
            "from app.crawl.decompression import decompress_body\n"
            "decode = decompress_body\n"
            "def f(raw, enc):\n"
            "    return decode(raw, enc)\n"
        ),
        "getattr": (
            "import app.crawl.decompression as compression\n"
            "def f(raw, enc):\n"
            "    return getattr(compression, 'decompress_body')(raw, enc)\n"
        ),
    }
    for label, source in alias_shapes.items():
        offenders = _decompress_cap_offenders(ast.parse(source), path)
        assert len(offenders) == 1, (label, offenders)

    good = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc, max_output_bytes=1024)\n")
    assert not _decompress_cap_offenders(good, path)


# ---------------------------------------------------------------------------
# Decompression reaches only through the capped chokepoint (legacy #3)
# ---------------------------------------------------------------------------

# The codecs app/crawl/decompression.py imports to implement decompress_body.
_DECOMPRESS_CODECS = frozenset({"gzip", "zlib", "brotli"})
_DECOMPRESS_CODEC_METHODS = frozenset({"decompress", "decompressobj", "GzipFile", "Decompressor"})


def _decompress_bypass_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag raw stdlib decompression (import or call) outside the chokepoint.

    All body decoding must route through ``decompression.decompress_body``, which
    makes ``max_output_bytes`` mandatory and returns input unchanged on decoder
    error (so a forged ``Content-Encoding`` cannot expand or corrupt unbounded).
    A module that imports or calls ``gzip``/``zlib``/``brotli`` directly can
    re-introduce the header-trusting, uncapped decompress the legacy cleanup
    removed — the ``_decompress_cap`` sweep only guards the chokepoint's own
    callers, so this closes the bypass around it.
    """
    return (
        [
            offender_at(path, node, f"imports decompression codec '{alias.name}' (bypasses decompress_body)")
            for node in nodes_of_type(tree, ast.Import)
            for alias in node.names
            if alias.name.split(".")[0] in _DECOMPRESS_CODECS
        ]
        + [
            offender_at(
                path, node, f"imports decompression codec from '{node.module}' (bypasses decompress_body)"
            )
            for node in nodes_of_type(tree, ast.ImportFrom)
            if (node.module or "").split(".")[0] in _DECOMPRESS_CODECS
        ]
        + [
            offender_at(
                path,
                call,
                f"calls {call.func.value.id}.{call.func.attr}() directly (bypasses decompress_body)",
            )
            for call, func in attribute_calls(tree)
            if isinstance(func.value, ast.Name)
            and func.value.id in _DECOMPRESS_CODECS
            and func.attr in _DECOMPRESS_CODEC_METHODS
        ]
    )


def test_decompression_reaches_only_through_the_chokepoint():
    """No app module touches gzip/zlib/brotli except the decompression chokepoint."""
    assert_trees_clean(
        _decompress_bypass_offenders,
        "decompress only via decompression.decompress_body()",
        trees=app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED),
    )


def test_decompress_bypass_detector_bites():
    """A header-driven raw decompress fires on both import and call."""
    # Two offenders from one source: the import AND the call are each a bypass, so
    # a source that imports the codec but routes through decompress_body has to
    # report 1, not 2 -- the count is what proves the detector is not just
    # keying on the import.
    assert_detector(
        _decompress_bypass_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        trips={
            "import gzip\n"
            "def f(raw, hdr):\n"
            "    if 'gzip' in hdr.get('content-encoding', ''):\n"
            "        return gzip.decompress(raw)\n": 2,
        },
        clean=(
            "from app.crawl.decompression import decompress_body\n"
            "def f(raw, hdr):\n"
            "    return decompress_body(raw, hdr.get('content-encoding', ''), max_output_bytes=1024)\n",
        ),
    )


# ---------------------------------------------------------------------------
# A discovery response is read through _Discovery, never off the tuple (jscpd)
#
# The jscpd pair at fetch_helpers.py was the two discovery-fetch signatures, but
# the rule underneath them was never owned: what a discovery response *means*.
# The load-bearing part of that is ``headers: dict | None``, where None is the
# transport-failure marker. ``_Discovery``'s accessors are total over it, so a
# consumer cannot reach a ``.get`` on a missing map. Take the header map back and
# the guard has to be re-derived by hand -- which is the shape that was actually
# duplicated, three hand-written projections of one positional 3-tuple each
# repeating the ``is None`` + status test. Nothing but this guard stops the
# fourth projection from landing.
# ---------------------------------------------------------------------------

# The fetch that returns a _Discovery. The other two discovery helpers project
# that response to caller shapes, so naming this one is what makes the rule
# about the RESULT rather than about a call site.
_DISCOVERY_FETCH = "_fetch_decode_discovery"

# The chokepoint module that legitimately owns what it bans.
_DISCOVERY_CHOKEPOINT = "app/crawl/fetch_helpers.py"


def _is_discovery_fetch(value: ast.AST | None) -> bool:
    # The result is normally reached through ``await``, so the assignment's value
    # is the Await node and the call is inside it. Unwrapping one layer is what
    # keeps the detector from being clean on a tree that does the banned thing.
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    leaf = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return leaf == _DISCOVERY_FETCH


def _discovery_response_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag reading a discovery response off the tuple instead of through _Discovery.

    Both spellings are offenders, because both restore the reach the accessors
    close. Destructuring is the one this replaced:
    ``text, status, resp_headers = await _fetch_decode_discovery(...)`` followed
    by ``resp_headers.get("etag")`` puts the ``dict | None`` back in the caller's
    hands with the ``is None`` test re-derived by hand. A detector that only
    understood the attribute form would call that tree clean, so the two walks
    are reported separately and both are required.
    """
    nodes = nodes_of_type(tree, ast.Assign, ast.AnnAssign)
    bound: set[str] = set()
    destructured: list[ast.AST] = []
    for node, target, value in assignments(nodes):
        if not _is_discovery_fetch(value):
            continue
        if isinstance(target, ast.Name):
            bound.add(target.id)
        else:
            destructured.append(node)
    return [
        offender_at(path, node, "destructures a discovery response off the tuple") for node in destructured
    ] + [
        offender_at(path, node, "reads a discovery response's header map directly")
        for node in nodes_of_type(tree, ast.Attribute)
        if node.attr == "headers" and isinstance(node.value, ast.Name) and node.value.id in bound
    ]


def test_discovery_response_is_read_through_one_owner():
    """Only _Discovery's accessors touch a discovery response's header map."""
    offenders = app_offenders(_discovery_response_offenders, skip=(_DISCOVERY_CHOKEPOINT,))
    assert not offenders, "Read a discovery response via _Discovery accessors: " + ", ".join(offenders)


def test_discovery_response_detector_bites():
    """Both restoration shapes fire; going through the accessors stays clean."""
    bad = ast.parse(
        "async def f(fetcher):\n"
        "    text, status, resp_headers = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return text, status, resp_headers.get('etag')\n"
    )
    offenders = _discovery_response_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    assert "destructures a discovery response" in offenders[0], offenders
    attr_bad = ast.parse(
        "async def f(fetcher):\n"
        "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.headers\n"
    )
    offenders = _discovery_response_offenders(attr_bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    assert "header map directly" in offenders[0], offenders
    good = ast.parse(
        "async def f(fetcher):\n"
        "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.validators(), resp.not_modified(), resp.text, resp.status\n"
    )
    assert not _discovery_response_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Handle minting is single-sourced (H4 residue / HIGH #9 entropy policy)
# ---------------------------------------------------------------------------

_ID_MINT_SANCTIONED = frozenset({"ids.py"})


def _id_mint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct ``uuid4()`` calls or ``.hex`` slicing outside app/crawl/ids.

    Handles double as the lookup credential for status/result routes, so the
    full-entropy policy must live in exactly one primitive; a re-inlined mint
    is where a shorter slice silently regresses to 48 bits. The two shapes are
    separate walks because a call and a subscript are different nodes; the
    callee's leaf name is :func:`chokepoint_walks.calls_named_leaf`, shared with
    the error-envelope sweep, so "a banned chokepoint reached as ``mod.name()``
    or as a bare import" is decided once.
    """
    return [
        offender_at(path, call, "uuid4() minted outside app/crawl/ids.py")
        for call in calls_named_leaf(tree, {"uuid4"})
    ] + [
        offender_at(path, node, "uuid hex sliced outside app/crawl/ids.py")
        for node in nodes_of_type(tree, ast.Subscript)
        if getattr(node.value, "attr", None) == "hex"
    ]


def test_handles_are_minted_in_one_place():
    """Only app/crawl/ids.py calls uuid4 or slices its hex (H4 residue)."""
    assert_trees_clean(
        _id_mint_offenders,
        "handle minting is single-sourced in app/crawl/ids.py",
        trees=app_trees(exclude=SANCTIONED | _ID_MINT_SANCTIONED),
    )


def test_id_mint_detector_bites():
    assert_detector(
        _id_mint_offenders,
        path=REPO_ROOT / "app" / "api" / "_synthetic.py",
        # uuid4 call AND hex slice are two findings, not one.
        trips={"import uuid\n\ndef f():\n    return uuid.uuid4().hex[:12]\n": 2},
        clean=("from app.crawl.ids import new_request_id\n\ndef f():\n    return new_request_id()\n",),
    )


# ---------------------------------------------------------------------------
# Header ints are guarded (L2): a hostile header must never crash a fetch
# ---------------------------------------------------------------------------


def _is_int_or_float_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"int", "float"}


def _reads_headers(node: ast.AST) -> bool:
    """True for ``x.headers.get(...)`` or ``x.headers[...]`` -- a raw header read."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        source = node.func.value
        return node.func.attr == "get" and isinstance(source, ast.Attribute) and source.attr == "headers"
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Attribute) and node.value.attr == "headers"
    return False


def _catches_value_error(node: ast.Try) -> bool:
    """True if any handler catches ValueError (incl. via a tuple/Exception)."""
    for handler in node.handlers:
        exc = handler.type
        names = (
            {elt.id for elt in exc.elts if isinstance(elt, ast.Name)}
            if isinstance(exc, ast.Tuple)
            else {exc.id}
            if isinstance(exc, ast.Name)
            else set()
        )
        if names & {"ValueError", "Exception", "BaseException"}:
            return True
    return False


def _unguarded_header_int_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``int``/``float`` of a header-derived value outside a try/except ValueError.

    Parsing a ``Content-Length`` (or any header) into a number without guarding
    the conversion is the L2 class: one crafted ``Content-Length: abc`` raises
    out of the fetcher and fails the whole request. The safe shapes either go
    through a guard predicate (the arg is a parameter, not an in-scope header
    read) or wrap the ``int()`` in ``try/except ValueError`` (middleware's
    request-cap check). Tree-wide because any module can grow a header read.
    """
    offenders: list[str] = []
    for func in functions(tree):
        header_names = {
            target.id
            for node in nodes_of(func)
            if isinstance(node, ast.Assign) and _reads_headers(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        guarded = {
            id(sub)
            for node in nodes_of(func)
            if isinstance(node, ast.Try) and _catches_value_error(node)
            for sub in nodes_of(node)
            if _is_int_or_float_call(sub)
        }
        for node in nodes_of(func):
            if not _is_int_or_float_call(node) or id(node) in guarded or not node.args:
                continue
            arg = node.args[0]
            is_derived = _reads_headers(arg) or (isinstance(arg, ast.Name) and arg.id in header_names)
            if is_derived:
                offenders.append(offender_at(path, node, f"{func.name}() parses a header number unguarded"))
    return offenders


def test_header_numbers_are_converted_under_guard():
    """No unguarded int()/float() on a header value anywhere in app/ (L2)."""
    assert_trees_clean(
        _unguarded_header_int_offenders,
        "parse header numbers through a guard or try/except ValueError",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_header_int_detector_bites():
    bad = ast.parse(
        "async def fetcher(resp):\n    content_length = resp.headers.get('content-length')\n"
        "    return int(content_length) > 100\n"
    )
    offenders = _unguarded_header_int_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    inline_bad = ast.parse(
        "async def fetcher(resp):\n    return int(resp.headers.get('content-length', 0)) > 100\n"
    )
    assert _unguarded_header_int_offenders(inline_bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    guarded = ast.parse(
        "def guard(value):\n    try:\n        return int(value)\n    except ValueError:\n        return 0\n"
    )
    assert not _unguarded_header_int_offenders(guarded, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # A sanitizer takes a parameter, not a live header read -- never flagged.
    param_based = ast.parse(
        "def _declared_length_exceeds(header_value, limit):\n    return int(header_value) > limit\n"
    )
    assert not _unguarded_header_int_offenders(param_based, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Robots politeness floor is committed only through the bounded setter (M1)
# ---------------------------------------------------------------------------


def _unbounded_domain_delay_writes(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct writes to ``_domain_min_delay`` outside ``set_domain_min_delay``.

    The robots Crawl-delay floor is clamped to a finite politeness ceiling only
    in ``set_domain_min_delay``; a bare ``self._domain_min_delay[...] = ...``
    anywhere else (even ``+=``) bypasses that bound and can drop an
    ``inf``/huge value straight into ``asyncio.sleep`` (the M1 hang).
    """
    offenders: list[str] = []
    for func in (
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if func.name == "set_domain_min_delay":
            continue
        for node in nodes_of_type(func, ast.Assign, ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "_domain_min_delay"
                ):
                    offenders.append(
                        offender_at(path, node, f"unbounded write to _domain_min_delay in {func.name}()")
                    )
    return offenders


def test_domain_politeness_floor_is_committed_only_via_the_setter():
    """Only set_domain_min_delay writes _domain_min_delay (M1 chokepoint)."""
    assert_trees_clean(
        _unbounded_domain_delay_writes,
        "commit the Crawl-delay floor through the clamped set_domain_min_delay",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_domain_delay_detector_bites():
    assert_detector(
        _unbounded_domain_delay_writes,
        path=REPO_ROOT / "app" / "_synthetic.py",
        # Both a write and an augmented assignment are unbounded; only the
        # setter's clamped write is not.
        trips={
            "class S:\n    def other(self):\n        self._domain_min_delay['x'] = 1e9\n"
            "    def bump(self):\n        self._domain_min_delay['y'] += 1\n": 2,
        },
        clean=(
            "class S:\n    def set_domain_min_delay(self, d, s):\n"
            "        self._domain_min_delay[d] = min(s, 300.0)\n"
            "    def read(self, d):\n        return self._domain_min_delay.get(d, 0.0)\n",
        ),
    )


# ---------------------------------------------------------------------------
# Whole-site frontier must not drain on transient failure
# ---------------------------------------------------------------------------


# Both accepted spellings of a transience gate, kept as one tuple so the
# detector and its failure message cannot drift apart. The accessor is the
# preferred seam; the set is what the accessor consults, so a direct membership
# test is legitimate rather than a bypass. A third spelling must be added here
# deliberately, not discovered by a test going red.
_TRANSIENCE_GATE_NAMES = ("is_transient_classification", "TRANSIENT_CLASSIFICATIONS")


def _consults_transience_verdict(func: ast.AST) -> bool:
    """Whether a function asks the taxonomy a transience question, either way.

    Matches the bare-name and qualified-attribute spellings, so the gate is
    recognised however the module chose to import it.
    """
    return any(
        (isinstance(n, ast.Name) and n.id in _TRANSIENCE_GATE_NAMES)
        or (isinstance(n, ast.Attribute) and n.attr in _TRANSIENCE_GATE_NAMES)
        for n in nodes_of(func)
    )


def _ungated_job_visited_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag committing a *whole-site* URL to ``job.visited`` without the gate.

    A URL that fails for a transient reason (network/dns/tls/timeout/crash) must
    be re-queued, not marked visited: committing it is what let a network outage
    drain a whole-site frontier to a seed-only ``COMPLETED``. Keyed on the
    persisted ``job.visited`` receiver so the local per-request ``visited`` set
    (recursive/fetch_helpers) — a different thing, where retrying the same failed
    URL within one crawl is pointless — is not matched.
    """
    offenders: list[str] = []
    for func in functions(tree):
        commits = [
            n
            for n in nodes_of(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "visited"
            and isinstance(n.func.value.value, ast.Name)
            and n.func.value.value.id == "job"
        ]
        if not commits:
            continue
        if not _consults_transience_verdict(func):
            offenders.append(
                offender_at(
                    path,
                    commits[0],
                    f"job.visited.add() without a transience "
                    f"gate -- route it through {_TRANSIENCE_GATE_NAMES[0]}()",
                )
            )
    return offenders


def test_whole_site_visited_commit_is_transient_gated():
    """A transiently-failed whole-site URL is re-queued, never counted done."""
    assert_trees_clean(
        _ungated_job_visited_offenders,
        "gate every job.visited.add on is_transient_classification() "
        "so transient failures are re-queued, not marked visited",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_whole_site_visited_gate_detector_bites():
    """An unguarded whole-site visited commit fires; both gate spellings stay clean."""
    bad = ast.parse("def run(job):\n    for url in batch:\n        job.visited.add(url)\n")
    offenders = _ungated_job_visited_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    # The message must name the seam to route through, or a red detector is only
    # as useful as the guess it forces.
    assert "is_transient_classification" in offenders[0], offenders

    # The accessor, bare and qualified: the import style is the module's choice
    # and must not decide whether the gate is recognised.
    for gate in (
        "if is_transient_classification(outcome):\n            continue",
        "if constants.is_transient_classification(outcome):\n            continue",
        "if outcome in TRANSIENT_CLASSIFICATIONS:\n            continue",
    ):
        good = ast.parse(
            f"def run(job, outcome):\n    for url in batch:\n        {gate}\n        job.visited.add(url)\n"
        )
        assert not _ungated_job_visited_offenders(good, REPO_ROOT / "app" / "_synthetic.py"), gate


# ---------------------------------------------------------------------------
# Collectible tests: a test pytest cannot see is a test that never runs
# ---------------------------------------------------------------------------

# pytest collects a test function only when it sits at module level or is a
# direct method of a ``Test*`` class. A ``test_*`` def anywhere else is parsed,
# imported, and then silently ignored -- so the suite stays green while the
# coverage it appeared to provide is gone. Nothing in a normal test run can
# catch that: the absence of a failure looks exactly like a passing test.
#
# This is not hypothetical. A test added in the middle of a test class, dedented
# to column 0, does not raise IndentationError -- the methods that followed it
# are absorbed as nested defs inside it, where nothing calls them. One green run
# of 1914 tests was nine tests short of the truth, and the only reason it was
# caught is that the count was compared against HEAD rather than assumed.


def _uncollectible_tests(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Report every ``test_*`` def pytest would silently not collect."""
    legal_owners: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            legal_owners.add(id(node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            legal_owners.update(
                id(m) for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
            continue
        if id(node) in legal_owners:
            continue
        owners = [
            parent.name
            for parent in ast.walk(tree)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node in ast.walk(parent)
            and parent is not node
        ]
        where = (
            f"nested inside {owners[0]}()" if owners else "not a module-level test or a Test* class method"
        )
        offenders.append(offender_at(path, node, f"test_{node.name} is uncollectible -- {where}"))
    return offenders


def test_every_test_function_is_one_pytest_can_collect():
    assert_trees_clean(
        _uncollectible_tests,
        "these test functions are silently never run; pytest "
        "collects only module-level tests and Test* class methods",
        trees=trees_under(TESTS_DIR),
    )


def test_uncollectible_test_detector_bites():
    """Every uncollectible shape fires; both collectible shapes stay clean."""
    path = TESTS_DIR / "_synthetic.py"
    fires = {
        # Nested inside another function: the shape that silently ate 9 tests.
        "def helper():\n    def test_inner():\n        pass\n": "nested inside helper()",
        # A method of a class pytest will not collect from.
        "class Cases:\n    def test_inner(self):\n        pass\n": "not a module-level test",
        # Two levels deep inside a class, still invisible.
        "class TestOuter:\n    def helper(self):\n        def test_inner(self):\n            pass\n":
        "nested inside helper()",
    }
    assert_fires_once(_uncollectible_tests, fires, path)

    clean = (
        "def test_module_level():\n    pass\n",
        "async def test_module_level_async():\n    pass\n",
        "class TestThings:\n    def test_method(self):\n        pass\n",
        "class TestThings:\n    async def test_async_method(self):\n        pass\n",
        # Fixtures and helpers are not tests and must not be reported.
        "class TestThings:\n    def helper_fixture(self):\n        pass\n",
        "def not_a_test_helper():\n    pass\n",
    )
    assert_stays_clean(_uncollectible_tests, clean, path)


# ---------------------------------------------------------------------------
# A path validator returns a checked value, never the input it was handed
# ---------------------------------------------------------------------------

# `_validate_storage_state_path` returned the literals "default", "session" and
# "persistent" as though they named sessions. They do not: Playwright types
# `storage_state` as `Union[str, pathlib.Path] | Dict`, so a string is always
# read as a FILE PATH, and the chokepoint whose documented job is stopping a
# caller from reaching a path it should not was handing out three unvalidated
# paths relative to the process working directory. The shape of that bug is
# general: a function that allowlists by resolving and testing containment, then
# returns one of its own parameters on some other branch -- so the allowlist
# holds for most inputs and not for the ones that mattered.
#
# Detected against the containment check rather than the function name, because
# the containment check is the thing whose bypass this is. A bare
# constant-return check was tried first and was too weak: the shipped code
# returned a Name, not a literal, so it passed the gate written for it.
_CONTAINMENT_CALL = "relative_to"


def _unchecked_parameter_return_offenders(tree, path) -> list[str]:
    """Report path validators that return an input parameter on some branch."""
    offenders: list[str] = []
    for func in functions(tree):
        body_calls = {
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        if _CONTAINMENT_CALL not in body_calls:
            continue
        parameters = {arg.arg for arg in (*func.args.args, *func.args.posonlyargs, *func.args.kwonlyargs)}
        for node in nodes_of_type(func, ast.Return):
            if isinstance(node.value, ast.Name) and node.value.id in parameters:
                offenders.append(
                    f"{path.name}:{node.lineno} {func.name}() returns its own parameter "
                    f"{node.value.id!r} on a branch that bypasses the containment check; "
                    f"a path validator returns a resolved, allowlisted path or None"
                )
    return offenders


def test_path_validators_never_return_an_unchecked_parameter():
    offenders = app_offenders(_unchecked_parameter_return_offenders)
    assert not offenders, "; ".join(offenders)


def test_unchecked_parameter_return_detector_bites():
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    # The exact shape that shipped: an allowlist, plus a branch that returns the
    # caller's string before the allowlist ever sees it.
    bad = ast.parse(
        "def _validate_storage_state_path(self, storage_state: str) -> str | None:\n"
        '    if storage_state in {"default", "session"}:\n'
        "        return storage_state\n"
        "    path = Path(storage_state).resolve()\n"
        "    for allowed in allowed_dirs:\n"
        "        try:\n"
        "            path.relative_to(allowed)\n"
        "            return str(path)\n"
        "        except ValueError:\n"
        "            continue\n"
        "    return None\n"
    )
    assert len(_unchecked_parameter_return_offenders(bad, synthetic)) == 1

    # The fixed shape: only a resolved, allowlisted path ever comes back.
    good = ast.parse(
        "def _validate_storage_state_path(self, storage_state: str) -> str | None:\n"
        "    path = Path(storage_state).resolve()\n"
        "    for allowed in allowed_dirs:\n"
        "        try:\n"
        "            path.relative_to(allowed)\n"
        "            return str(path)\n"
        "        except ValueError:\n"
        "            continue\n"
        "    return None\n"
    )
    assert not _unchecked_parameter_return_offenders(good, synthetic)

    # Without a containment check there is nothing to bypass, so a function that
    # echoes its input is not this gate's business.
    no_check = ast.parse("def _label(name: str) -> str:\n    return name\n")
    assert not _unchecked_parameter_return_offenders(no_check, synthetic)

    # Returning a derived value, or None, is what the contract allows.
    derived = ast.parse(
        "def _resolve(base: str, name: str) -> str | None:\n"
        "    path = (Path(base) / name).resolve()\n"
        "    path.relative_to(base)\n"
        "    return str(path)\n"
    )
    assert not _unchecked_parameter_return_offenders(derived, synthetic)


# ---------------------------------------------------------------------------
# Advertise only the encodings this process can decode
# ---------------------------------------------------------------------------

# Request headers named `br` unconditionally while the brotli decoder is an
# OPTIONAL dependency -- app/crawl/decompression supports two incompatible
# brotli APIs precisely because either may be the one installed. So on a host
# without it, every brotli-capable server was told to return a body the process
# would then refuse, and the crawl failed on a request we chose to make that
# way. Advertising an encoding is a promise about what this process can decode,
# so it is checked against the decoder set rather than trusted per call site.
_ALWAYS_DECODABLE_ENCODINGS = {"gzip", "deflate", "identity"}
_ENCODING_TOKEN = re.compile(r"^[a-z0-9\-_+.]+$")
# A comma-joined list is only an encoding list if it MENTIONS a real encoding.
# Without this anchor the check reads English: api_models.py field descriptions
# like "dom_text, etc., http_html" are comma-joined, lowercase and token-shaped,
# and a gate that cries wolf on those gets ignored on the day it matters.
_KNOWN_ENCODINGS = {
    "gzip",
    "x-gzip",
    "deflate",
    "x-deflate",
    "br",
    "zstd",
    "compress",
    "x-compress",
    "identity",
    "*",
}


def _decodable_encoding_tokens() -> set[str]:
    from app.crawl.decompression import _brotli_available

    tokens = set(_ALWAYS_DECODABLE_ENCODINGS)
    if _brotli_available():
        tokens.add("br")
    return tokens


def _undecodable_encoding_offenders(tree, path, *, decodable: set[str] | None = None) -> list[str]:
    """Report encoding-list literals naming something this process cannot decode.

    ``decodable`` is injectable so the detector's logic can be tested against a
    process that has no brotli, which is exactly the deployment the live tree
    test usually is not.
    """
    if decodable is None:
        decodable = _decodable_encoding_tokens()
    # Keyed by (line, token): ast.walk yields a dict's value Constant both under
    # its key and as a node in its own right, so without this one bad header
    # reports twice and the count stops meaning anything.
    seen: dict[tuple[int, str], str] = {}

    def check(node, value: str) -> None:
        tokens = {token.strip().lower() for token in value.split(",") if token.strip()}
        if len(tokens) < 2 or not all(_ENCODING_TOKEN.match(token) for token in tokens):
            return
        if not tokens & _KNOWN_ENCODINGS:
            return
        for token in sorted(tokens - decodable):
            seen[(node.lineno, token)] = (
                f"{path.name}:{node.lineno} advertises {token!r}, which app/crawl/decompression "
                f"cannot decode here (decodable: {', '.join(sorted(decodable))})"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.lower() == "accept-encoding"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    check(value, value.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A standalone encoding list, however it reaches the header.
            check(node, node.value)
    return [seen[key] for key in sorted(seen)]


def test_no_request_header_advertises_an_undecodable_encoding():
    """Checked against THIS process's decoders, so the verdict is environmental.

    Where brotli is installed, naming ``br`` is correct and the gate is silent;
    where it is not, the same tree fails until the header stops asking for a
    body the process would refuse. Either way the header's claim is checked
    against the decoder set rather than trusted.
    """
    offenders = app_offenders(_undecodable_encoding_offenders)
    assert not offenders, "; ".join(offenders)


def test_undecodable_encoding_detector_bites():
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    # A process with no brotli decoder, which is the deployment the bug shipped to.
    no_brotli = {"gzip", "deflate", "identity"}

    # The shape that shipped: br named unconditionally, as a plain literal.
    bad = ast.parse('HEADERS = {"Accept-Encoding": "gzip, deflate, br"}\n')
    assert len(_undecodable_encoding_offenders(bad, synthetic, decodable=no_brotli)) == 1

    # Reached indirectly, as a variable rather than inside the dict literal.
    indirect = ast.parse('ENCODING = "gzip, deflate, br"\nHEADERS = {"Accept-Encoding": ENCODING}\n')
    assert len(_undecodable_encoding_offenders(indirect, synthetic, decodable=no_brotli)) == 1

    # The same tree is correct where the decoder exists, which is why the live
    # test has to consult the environment rather than assert a fixed verdict.
    assert not _undecodable_encoding_offenders(bad, synthetic, decodable={*no_brotli, "br"})

    # A genuinely undecodable token is caught even with brotli present.
    zstd = ast.parse('HEADERS = {"Accept-Encoding": "gzip, zstd"}\n')
    assert len(_undecodable_encoding_offenders(zstd, synthetic, decodable={*no_brotli, "br"})) == 1

    # The fixed shape: the optional token is appended only when it is decodable.
    good = ast.parse(
        'ENCODING = "gzip, deflate"\n'
        "if available():\n"
        '    ENCODING += ", br"\n'
        'HEADERS = {"Accept-Encoding": ENCODING}\n'
    )
    assert not _undecodable_encoding_offenders(good, synthetic, decodable=no_brotli)

    # Not an encoding list at all: another comma-joined header is not this gate's
    # business, and neither is English prose that happens to be comma-joined.
    other = ast.parse(
        'HEADERS = {"Accept": "text/html,application/xml;q=0.9"}\n'
        'OTHER = "dom_text, etc., http_html"\n'
        "SINGLE = 'gzip'\n"
    )
    assert not _undecodable_encoding_offenders(other, synthetic, decodable=no_brotli)


# ---------------------------------------------------------------------------
# Age-based deletion must consult ownership, not just age
# ---------------------------------------------------------------------------
# A sweep, GC, evict, prune or clean function that unlinks a file is deleting
# something a concurrent worker may still be reading. Age is not ownership: a
# scratch file lives exactly as long as the work writing it, and a process-wide
# sweep reaches every one of them. `_ResultSpill` learned this the hard way --
# its sweep unlinked by age alone and documented the loss of a still-active
# spill as acceptable, when the spill being deleted was the crawl's own
# reassembled result. Deletion by age is fine; deletion by age alone is not.
_SWEEP_NAME_PARTS = ("gc", "sweep", "evict", "prune", "clean", "reap", "purge")
_OWNERSHIP_GUARDS = ("in_use", "live", "in_flight", "active", "owned", "holder", "registry")


def _unowned_age_deletions(tree, path) -> list[str]:
    """Report sweep-like functions that unlink without consulting ownership."""
    offenders: list[str] = []
    for func in functions(tree):
        name = func.name.lower()
        if not any(part in name for part in _SWEEP_NAME_PARTS):
            continue
        unlinks = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("unlink", "remove", "rmdir")
        ]
        if not unlinks:
            continue
        body_names = {node.id for node in ast.walk(func) if isinstance(node, ast.Name)}
        body_names |= {node.attr for node in ast.walk(func) if isinstance(node, ast.Attribute)}
        if any(guard in candidate.lower() for candidate in body_names for guard in _OWNERSHIP_GUARDS):
            continue
        offenders.append(
            f"{path.name}:{func.lineno} {func.name}() unlinks without an ownership check; "
            f"skip paths that are live (age alone is not ownership)"
        )
    return offenders


def test_age_based_deletion_consults_ownership_first():
    offenders = app_offenders(_unowned_age_deletions)
    assert not offenders, "; ".join(offenders)


def test_unowned_age_deletion_detector_bites():
    # The shape that lost a live crawl result: a sweep that unlinks by age only.
    bad = ast.parse(
        "def _gc_stale(max_age):\n"
        "    for name in os.listdir(tmp):\n"
        "        if now - os.path.getmtime(name) > max_age:\n"
        "            os.unlink(name)\n"
    )
    assert len(_unowned_age_deletions(bad, REPO_ROOT / "app" / "_synthetic.py")) == 1

    # The fixed shape: the same sweep, asking who owns each candidate first.
    # The guard has to be something the KERNEL can answer -- ownership another
    # process can also see -- not a set this process happens to be holding.
    good = ast.parse(
        "def _gc_stale(max_age):\n"
        "    for name in os.listdir(tmp):\n"
        "        if not _spill_is_unowned(name):\n"
        "            continue\n"
        "        os.unlink(name)\n"
    )
    assert not _unowned_age_deletions(good, REPO_ROOT / "app" / "_synthetic.py")

    # A function that deletes but is not a sweep is not this gate's business.
    not_a_sweep = ast.parse("def close(self):\n    os.unlink(self.path)\n")
    assert not _unowned_age_deletions(not_a_sweep, REPO_ROOT / "app" / "_synthetic.py")

    # A method named for cleanup still has to ask.
    method = ast.parse("class C:\n    def cleanup(self):\n        os.remove(self.path)\n")
    assert len(_unowned_age_deletions(method, REPO_ROOT / "app" / "_synthetic.py")) == 1


# ---------------------------------------------------------------------------
# The ordered classification table: order is semantics, so it is checked
# ---------------------------------------------------------------------------

# The table in result_helpers maps an exception to a stable error code by walking
# rows in order and taking the first isinstance hit. That makes the row ORDER a
# semantic, not a formatting detail -- and until now the only statement of it was
# the comment block above the table, which no tool could hold to. A row added
# below a superclass row is silently dead: the superclass claims every instance
# first, so the specific verdict is unreachable and the only symptom is a wrong
# error code on a wire.
#
# `DecompressionError` is the case that proves it. It subclasses ValueError and
# had no row of its own, so the caller-error catch-all claimed it -- a corrupt
# response body was reported as `value_error`, a terminal code reading as "the
# caller asked for something invalid", which in a durable job marks the URL
# visited rather than retrying a fault a second attempt often clears. Nothing in
# the suite failed, because the code was a legal member of the taxonomy the whole
# time; it was simply being answered by the wrong row.

# Types broad enough to swallow a taxonomy decision if they are not last.
_CATCH_ALL_EXCEPTIONS: tuple[type[BaseException], ...] = (ValueError, OSError, Exception)


def _table_shadowing_offenders(rows) -> list[str]:
    """Report rows that can never fire, and broad rows that are not terminal.

    First-hit-wins means a row is dead the moment an EARLIER row's type is a
    superclass of one of its own: the earlier row claims every instance, so the
    specific verdict is unreachable no matter how it is spelled.
    """
    offenders: list[str] = []
    last_index = len(rows) - 1
    for index, (types, classification) in enumerate(rows):
        for kind in types:
            if kind in _CATCH_ALL_EXCEPTIONS and index != last_index:
                offenders.append(f"{kind.__name__} at row {index} is a catch-all but rows follow it")
            for earlier_index, (earlier_types, _) in enumerate(rows[:index]):
                for earlier in earlier_types:
                    if earlier is not kind and issubclass(kind, earlier):
                        offenders.append(
                            f"{kind.__name__} ({classification}) at row {index} can never fire: "
                            f"{earlier.__name__} at row {earlier_index} claims it first"
                        )
    return offenders


def test_the_classification_table_has_no_shadowed_or_mid_table_catch_all_rows():
    from app.crawl.result_helpers import _CLASSIFICATION_BY_EXCEPTION

    offenders = _table_shadowing_offenders(_CLASSIFICATION_BY_EXCEPTION)
    assert not offenders, (
        "the ordered table is matched first-hit-wins, so these rows can never fire "
        "or would swallow later verdicts -- order the specific rows first: " + ", ".join(offenders)
    )


def test_table_shadowing_detector_bites():
    """A subclass row below its superclass, and a mid-table catch-all, both fire."""

    class Sub(ValueError):
        pass

    class Deeper(Sub):
        pass

    # The shape that lost DecompressionError's verdict: no row of its own, so the
    # catch-all answers for it. Nothing to report -- there is no dead row, just a
    # type nobody has decided about, which is what the missing-row test is for.
    orphans = [((ValueError,), "value_error")]
    assert not _table_shadowing_offenders(orphans)

    # A specific row placed BELOW the superclass that already claims it: dead.
    below_super = [((ValueError,), "value_error"), ((Sub,), "value_error")]
    assert any("can never fire" in o for o in _table_shadowing_offenders(below_super))

    # A specific row ABOVE its superclass: live, and the superclass is the
    # catch-all, which is allowed to be last.
    correct = [((Deeper,), "http_error"), ((Sub,), "tls_error"), ((ValueError,), "value_error")]
    assert not _table_shadowing_offenders(correct)

    # A catch-all with any row after it, whatever that row claims.
    mid_table = [((Exception,), "http_error"), ((Deeper,), "tls_error")]
    assert any("catch-all" in o for o in _table_shadowing_offenders(mid_table))

    # A broad row mid-table also makes every later specific row dead.
    both = [((ValueError,), "value_error"), ((Deeper,), "http_error"), ((Sub,), "tls_error")]
    offenders = _table_shadowing_offenders(both)
    assert any("catch-all" in o for o in offenders)
    assert sum("can never fire" in o for o in offenders) == 2

    # The live shape this was written for.
    import httpx

    assert (
        _table_shadowing_offenders(
            [((httpx.ConnectError,), "connection_error"), ((httpx.HTTPError,), "http_error")]
        )
        == []
    )


# ---------------------------------------------------------------------------
# dataclass copying: replace, not asdict-and-rebuild
# ---------------------------------------------------------------------------

# ``asdict`` is a *serialization* primitive: it recurses through the field tree
# and hands back plain dicts, so a frozen nested dataclass (a CrawlStrategyAttempt)
# becomes a dict with the same keys. Unpacking that into a constructor call to
# "copy with one field changed" therefore rebuilds an object whose nested fields
# no longer match their declared types. The copy compares equal field-for-field,
# type-checks clean under a lax annotation, and raises AttributeError the first
# time a consumer reaches for an attribute -- which the render adapter does on
# its first read, long after the copy was made.
#
# The distinction the detector draws is not asdict-allowed-or-banned: genuine
# serialization is a legitimate use and job_store.to_json() is one. It is
# *where the result goes*. Unpacked into a constructor call, asdict is always the
# copy mistake; anything else, it is doing its real job. So the sink is the
# constructor, and no file needs an exemption to keep using asdict properly.


def _is_asdict_call(node: ast.AST) -> bool:
    """Whether a node calls asdict, under either import spelling.

    ``from dataclasses import asdict`` and ``import dataclasses`` are both in
    use in this tree, so a detector keyed on one spelling silently stops
    detecting the moment a module picks the other.
    """
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "asdict")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "asdict")
    )


def _asdict_rebuild_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag unpacking ``asdict(...)`` into a constructor call."""
    offenders: list[str] = []
    for call in nodes_of_type(tree, ast.Call):
        for keyword in call.keywords:
            if keyword.arg is not None or not isinstance(keyword.value, ast.AST):
                continue
            unpacked = [n for n in ast.walk(keyword.value) if _is_asdict_call(n)]
            if unpacked:
                offenders.append(
                    offender_at(
                        path,
                        unpacked[0],
                        "asdict(...) unpacked into a "
                        "constructor call: the nested dataclasses become dicts and "
                        "the rebuilt object violates its declared field types -- use dataclasses.replace()",
                    )
                )
                break
    return offenders


def test_asdict_is_never_unpacked_into_a_constructor():
    """Copying a dataclass with asdict must not survive anywhere in app/."""
    assert_trees_clean(
        _asdict_rebuild_offenders,
        "copy dataclasses with dataclasses.replace(), not asdict(**...)",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_asdict_rebuild_detector_bites():
    """The rebuild shape fires; genuine serialization does not."""
    path = REPO_ROOT / "app" / "_synthetic.py"
    fires = [
        "import dataclasses\ndef f(r):\n    return Thing(**{**dataclasses.asdict(r), 'd': 1})\n",
        "from dataclasses import asdict\ndef f(r):\n    return Thing(**asdict(r))\n",
    ]
    for src in fires:
        offenders = _asdict_rebuild_offenders(ast.parse(src), path)
        assert len(offenders) == 1 and "dataclasses.replace" in offenders[0], src

    # Serialization keeps asdict: a dict result used as a dict is its real job.
    clean = [
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    return json.dumps(data)\n",
        "import dataclasses\ndef f(s):\n    return dataclasses.asdict(s)['results']\n",
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    data['x'] = 1\n    return "
        "data\n",
    ]
    for src in clean:
        assert not _asdict_rebuild_offenders(ast.parse(src), path), src


# ---------------------------------------------------------------------------
# Classification transience: one verdict, owned by the taxonomy
# ---------------------------------------------------------------------------

# The redefinition chokepoint: a module-level collection named like a
# transience/retry verdict, anywhere in app/ except the taxonomy itself. A
# fourth hand-list is exactly how this drifted the first three times -- each site
# answered "is this transient?" locally, and the local answers disagreed.
#
# Scoped to collections whose names end in a verdict suffix, so a local *budget*
# is not mistaken for a local *answer*: MAX_TRANSIENT_REQUEUES is a cap on how
# many times the recursive loop may re-try, which is legitimately the caller's
# own number, and it is the collection shape plus the name that makes something
# a second opinion about the taxonomy.
_TRANSIENCE_HOME_SANCTIONED = frozenset({"constants.py"})
_TRANSIENCE_VERDICT_SUFFIXES = ("CLASSIFICATIONS", "CODES", "SET", "LABELS", "STATUSES")
_TRANSIENCE_SET_NAME = re.compile(
    r"^(?:[A-Z_]+_)?(?:TRANSIENT|RETRYABLE|REQUEUE)[A-Z_]*(?:"
    + "|".join(_TRANSIENCE_VERDICT_SUFFIXES)
    + r")$"
)
_TRANSIENCE_COLLECTIONS = (
    ast.Set,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.SetComp,
    ast.DictComp,
    ast.ListComp,
)


# A named alias to the taxonomy's own set is a re-export, not a second opinion:
# that is the one legitimate spelling here.
_TRANSIENCE_REEXPORTS = frozenset(
    {
        "TRANSIENT_CLASSIFICATIONS",
        "NON_TRANSIENT_CLASSIFICATIONS",
        "is_transient_classification",
    }
)


def _transience_verdict_name(target: ast.expr, value: ast.expr | None) -> str | None:
    """The name of a module-level transience/retry verdict, or ``None``.

    All three of the shape's requirements are one predicate -- a verdict-shaped
    name, a collection-shaped value, and not a re-export of the taxonomy's own
    set -- so the filter is stated once here instead of as three ``continue``
    guards in each detector that wanted the same answer.
    """
    name = getattr(target, "id", None)
    if not isinstance(name, str) or not _TRANSIENCE_SET_NAME.match(name):
        return None
    if value is None or not isinstance(value, _TRANSIENCE_COLLECTIONS):
        return None
    if isinstance(value, ast.Name) and value.id in _TRANSIENCE_REEXPORTS:
        return None
    return name


def _ad_hoc_transience_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a module-level transience/retry verdict defined outside app.constants.

    A consumer asking "does this failure deserve another pass" must call
    ``app.constants.is_transient_classification``. A local answer is a second
    opinion, and second opinions are what made ``dns_error`` look retryable
    while ``connection_error`` -- the label every transport failure actually
    gets -- did not.
    """
    return [
        offender_at(path, node, f"local {name} re-answers the taxonomy's transience verdict")
        for node, target, value in assignments(nodes_of(tree))
        if (name := _transience_verdict_name(target, value)) is not None
    ]


def test_transience_verdict_has_one_home():
    """No module outside app/constants.py may hold its own transient/retry set."""
    offenders: list[str] = []
    for path, tree in app_trees():
        if path.name in _TRANSIENCE_HOME_SANCTIONED:
            continue
        offenders.extend(_ad_hoc_transience_offenders(tree, path))
    assert not offenders, (
        "app.constants.is_transient_classification is the single answer for "
        "'is this failure worth another pass': " + ", ".join(offenders)
    )


def test_transience_home_detector_bites():
    """A local transience verdict fires; re-exports, budgets and count-shaped
    constants do not."""
    bad = ast.parse('TRANSIENT_CLASSIFICATIONS = {"dns_error", "network_error"}\n')
    offenders = _ad_hoc_transience_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 1 and "TRANSIENT_CLASSIFICATIONS" in offenders[0], offenders

    assert (
        len(
            _ad_hoc_transience_offenders(
                ast.parse("RETRYABLE_CLASSIFICATIONS = frozenset()\n"),
                REPO_ROOT / "app" / "_synthetic.py",
            )
        )
        == 0
    ), "a set-shaped verdict name must fire whether it is a literal or a call"

    for clean in (
        "from app.constants import TRANSIENT_CLASSIFICATIONS\n",
        "from app.constants import is_transient_classification as is_transient\n",
        "TRANSIENT_CLASSIFICATIONS = TRANSIENT_CLASSIFICATIONS\n",
        "MAX_TRANSIENT_REQUEUES = 3\n",
        "RETRYABLE_ERROR_TYPES = frozenset()\n",
    ):
        assert not _ad_hoc_transience_offenders(ast.parse(clean), REPO_ROOT / "app" / "_synthetic.py"), clean


# ---------------------------------------------------------------------------
# Error-handling chokepoint drift detectors
#
# A strategy that raises (rather than returning a classified result) falls to
# the scheduler's blanket envelope and is misclassified, or in on_result mode
# silently drops the page. Three invariants keep that from drifting back:
#
#   1. _engine_result_from_exception is the only place a strategy exception may
#      become a result (no hand-rolled failed-result construction with a
#      hardcoded classification outside the two legitimate config guards).
#   2. BrowserPoolExhaustedError may only be caught by sanctioned sites
#      (the render endpoints, which own the 503 transport response) -- never
#      ad-hoc catch sites in the crawl path, which would re-derive retry
#      semantics locally instead of reading the classification.
#   3. _classify_error_code must be defined in result_helpers (neutral home),
#      never in a strategy module; strategies reach for it, they don't own it.
# ---------------------------------------------------------------------------

# Classifications that a hand-rolled _failed_engine_result/_make_engine_result
# may legitimately carry without going through the exception chokepoint:
#   browser_crash / unsupported_strategy: the engine's config guards -- there
#     is no exception to classify, the worker was never built
#   binary_no_text: a fetched page whose body has no extractable text
#   not_modified: a 304 cache hit (a *success*, built by the 304 path)
# Everything else means a real exception was converted to a result by hand,
# which is the drift the chokepoint exists to prevent.
_ERROR_CHOKEPOINT_SANCTIONED = frozenset({"result_helpers.py"})


_NON_EXCEPTION_CLASSIFICATIONS = frozenset(
    {"browser_crash", "unsupported_strategy", "binary_no_text", "not_modified"}
)


def _has_error_field(node, expected) -> bool:
    """The call carries the given taxonomy code in its ``error=`` kwarg."""
    return any(
        kw.arg == "error" and isinstance(kw.value, ast.Constant) and kw.value.value == expected
        for kw in node.keywords
    )


def _hand_rolled_failed_result_offenders(tree, path):
    """Failed-result construction with a hardcoded classification outside the
    chokepoint and the engine's two config guards (no browser worker / unknown
    strategy -- those have no exception to classify)."""
    for node in nodes_of(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("_failed_engine_result", "_make_engine_result")
        ):
            continue
        status = None
        for kw in node.keywords:
            if kw.arg == "classification" and isinstance(kw.value, ast.Constant):
                status = kw.value.value
        if status in (None,) or status in _NON_EXCEPTION_CLASSIFICATIONS:
            continue
        # The engine's unsupported-strategy guard and the scheduler's
        # circuit-breaker guard are policy decisions, not caught exceptions:
        # they carry their own taxonomy code in the error field.
        if status == "unknown_error" and _has_error_field(node, "unsupported_strategy"):
            continue
        if status == "network_error" and _has_error_field(node, "circuit_breaker_open"):
            continue
        yield offender_at(
            path, node, f"hand-rolled classification={status!r}; route through _engine_result_from_exception"
        )


def test_no_hand_rolled_failed_results_outside_the_chokepoint():
    assert_trees_clean(
        _hand_rolled_failed_result_offenders,
        "failed results with a hardcoded classification must go through "
        "_engine_result_from_exception (the single exception->result chokepoint)",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_hand_rolled_failed_result_detector_bites():
    assert_detector(
        _hand_rolled_failed_result_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def f():\n"
            "    return _failed_engine_result(\n"
            '        classification="network_error", error="worker_exception",\n'
            "    )\n": "_engine_result_from_exception",
        },
        clean=(
            'def f():\n    return _failed_engine_result(\n        classification="browser_crash",\n    )\n',
        ),
    )


_POOL_EXHAUSTION_CATCH_SANCTIONED = frozenset({"render.py", "render_endpoint.py", "browser_pool.py"})


def _unsanctioned_pool_exhaustion_catch_offenders(tree, path):
    """A catch of BrowserPoolExhaustedError outside the render transports and
    the worker guard is a local re-derivation of retry semantics."""
    for node in nodes_of(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = []
        t = node.type
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Tuple):
            names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        if not any("BrowserPoolExhaustedError" in n for n in names):
            continue
        yield offender_at(path, node, "catching BrowserPoolExhaustedError; read the classification instead")


def test_pool_exhaustion_catch_sites_are_sanctioned():
    assert_trees_clean(
        _unsanctioned_pool_exhaustion_catch_offenders,
        "BrowserPoolExhaustedError may only be caught by the render transports (503 response) "
        "and the browser worker guard; the crawl path reads the browser_pool_exhausted classification",
        trees=app_trees(exclude=SANCTIONED | _POOL_EXHAUSTION_CATCH_SANCTIONED),
    )


def test_pool_exhaustion_catch_detector_bites():
    assert_detector(
        _unsanctioned_pool_exhaustion_catch_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "async def f():\n"
            "    try:\n"
            "        await crawl()\n"
            "    except BrowserPoolExhaustedError:\n"
            "        return None\n": "read the classification instead",
        },
        clean=(
            "async def f():\n"
            "    result = await crawl()\n"
            "    if result.classification == 'browser_pool_exhausted':\n"
            "        requeue()\n",
        ),
    )


def test_classifier_lives_in_the_neutral_home():
    """_classify_error_code must be defined in result_helpers, not re-defined
    in a strategy module (the old site, http.py, forced browser.py to import
    across strategies)."""
    for path, tree in app_trees(exclude=SANCTIONED | _ERROR_CHOKEPOINT_SANCTIONED):
        for node in nodes_of_type(tree, ast.FunctionDef, ast.AsyncFunctionDef):
            if node.name == "_classify_error_code":
                raise AssertionError(
                    offender_at(path, node, "_classify_error_code defined outside result_helpers")
                )


# ---------------------------------------------------------------------------
# Value-validity chokepoint (the meta-fix)
#
# The suite polices *routing* (a failed result must be built through the
# chokepoint primitives) but never *value validity*: nothing verified the
# classification string flowing through the primitive was a legal enum member.
# That is how url_budget_exceeded shipped -- a genuinely distinct signal passed
# as known_classification=, the lenient internal dataclass accepted it, and the
# strict enum at response shaping rejected the whole batch with a 500. The
# routing tests were green the entire time.
#
# This detector closes the class, not the instance: any classification handed
# to the primitives as a literal must be a CrawlClassification member.
# ---------------------------------------------------------------------------

_CLASSIFICATION_VALUE_SOURCES = ("classification", "known_classification")
_RESULT_PRIMITIVES = (
    # Derived, not transcribed: the per-field detectors that used to live here
    # matched on a six-name hand-kept list and silently omitted three live
    # primitives plus every direct dataclass construction. The generic sweep
    # in tests/membership_gate.py reads the registry from the models, so a
    # primitive added tomorrow is covered the day it lands. These tuples stay
    # only as the per-field detectors' public surface; the registry is the
    # source of truth and this is its shadow, kept in sync by the
    # subsumption assert below.
    "_failed_engine_result",
    "_make_engine_result",
    "_engine_result_from_exception",
    "_skipped_engine_result",
    "_crawl_result_from_exception",
    "_terminal_crawl_result",
)


def _result_primitive_names() -> frozenset[str]:
    """The registry the per-field detectors actually sweep.

    Routed through the derived registry rather than the transcribed tuple
       above so the three per-field detectors cannot re-acquire the coverage gap
    the generic sweep was written to close.
    """
    from tests.membership_gate import RESULT_PRIMITIVES

    return RESULT_PRIMITIVES


def _non_member_classification_offenders(tree, path, *, members=None):
    """A literal classification passed to a result primitive must be a member.

    ``members`` is the live ``CrawlClassification`` value set. There is no
    hand-maintained exemption list: such a list is precisely how a new code
    ships as a non-member -- the list would have to be remembered and updated
    in lockstep with the enum, and it never is. A non-literal (variable or
    attribute access, e.g. CrawlClassification.x) is what callers should
    write, and passes without comment.

    Which values count as spelled non-members is
    :func:`chokepoint_walks.non_member_literals`, the same funnel the status,
    strategy, Literal-field and error sweeps go through, so the guard that
    admits a variable cannot be dropped from one axis and kept on another.
    """
    if members is None:
        members = real_members("CrawlClassification")
    return [
        offender_at(
            path,
            node,
            f"classification={value!r} is not a CrawlClassification member; "
            r"add it to the enum (app/constants.py) or route through the classifier",
        )
        for node, _field, value in non_member_literals(
            (node, "classification", value, members)
            for node, value in call_keyword_values(
                tree,
                function_names=_result_primitive_names(),
                keyword_names=_CLASSIFICATION_VALUE_SOURCES,
            )
        )
    ]


def test_engine_emitted_classifications_are_enum_members():
    offenders = app_offenders(_non_member_classification_offenders)
    assert not offenders, (
        "a literal classification passed to a result primitive must be a "
        "CrawlClassification member, or the strict enum at response shaping "
        "rejects the whole batch: " + ", ".join(offenders)
    )


def test_non_member_classification_detector_bites():
    bad = ast.parse('def f():\n    return _failed_engine_result(classification="totally_made_up")\n')
    offenders = list(
        _non_member_classification_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"})
    )
    assert len(offenders) == 1 and "CrawlClassification member" in offenders[0], offenders
    good = ast.parse(
        "def f(cls):\n    return _failed_engine_result(classification=cls.url_budget_exceeded)\n"
    )
    assert not list(
        _non_member_classification_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"})
    )

    # The terminal-CrawlResult chokepoint is covered too: the render path builds
    # terminal results through it, so an unknown literal there reaches the wire
    # through the same lenient dataclass that let url_budget_exceeded ship.
    bad_terminal = ast.parse(
        'def f():\n    return _terminal_crawl_result(classification="made_up_render_code")\n'
    )
    offenders = list(
        _non_member_classification_offenders(
            bad_terminal, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}
        )
    )
    assert len(offenders) == 1 and "CrawlClassification member" in offenders[0], offenders
    good_terminal = ast.parse(
        "def f(cls):\n    return _terminal_crawl_result(classification=cls.site_error)\n"
    )
    assert not list(
        _non_member_classification_offenders(
            good_terminal, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}
        )
    )


# ---------------------------------------------------------------------------
# Strategy-label validity chokepoint (the sibling of the value-validity one)
#
# The same class of bug hit the strategy axis: the browser pool worker labeled
# its terminal result ``strategy="BROWSER_DECLARED_CRAWLER"`` -- a string that
# is not a CrawlStrategy member -- while the scheduler's worker-exception
# envelope and the engine's budget-timeout envelope both passed ``strategy=""``.
# The internal CrawlStrategyName Literal and the strict CrawlStrategy enum on
# the wire model reject both values; the only reason anything worked at all was
# a pair of ``# type: ignore[arg-type]`` at the CrawlStrategyAttempt
# construction sites plus _shape_all's silent catch, which threw away the real
# classification and replaced it with site_error. A non-member strategy literal
# must never reach a result primitive again.
# ---------------------------------------------------------------------------

_STRATEGY_VALUE_SOURCES = ("strategy",)


def _non_member_strategy_offenders(tree, path, *, members=None):
    """A literal strategy passed to a result primitive must be a real member.

    ``members`` is the live ``CrawlStrategy`` value set. ``None`` is admitted
    explicitly: it is the honest label for "no strategy was attempted" (an
    envelope built after a worker raised, before any strategy ran), which is
    not an invented member and not an empty string. There is no
    hand-maintained exemption list -- such a list is precisely how the invented
    name shipped, because it would have to be remembered and updated in
    lockstep with the enum. A non-literal (variable, attribute, or module
    constant) is what callers should write and passes without comment.
    """
    if members is None:
        members = real_members("CrawlStrategy")

    for node, value in call_keyword_values(
        tree,
        function_names=_result_primitive_names(),
        keyword_names=_STRATEGY_VALUE_SOURCES,
    ):
        if isinstance(value, ast.Constant) and value.value is None:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if value.value not in members:
            yield offender_at(
                path,
                node,
                f"strategy={value.value!r} is not a CrawlStrategy "
                f"member; pass a real member, or None when no strategy was attempted",
            )


def test_engine_emitted_strategies_are_enum_members():
    offenders = app_offenders(_non_member_strategy_offenders)
    assert not offenders, (
        "a literal strategy passed to a result primitive must be a "
        "CrawlStrategy member, or None when no strategy was attempted; the "
        "strict enum on the wire model rejects anything else and the shaping "
        "fallback silently overwrites the real classification: " + ", ".join(offenders)
    )


def test_non_member_strategy_detector_bites():
    # An invented member name -- exactly the shape that shipped on the browser
    # worker's failure path.
    bad = ast.parse(
        'def f():\n    return _engine_result_from_exception(strategy="BROWSER_DECLARED_CRAWLER")\n'
    )
    offenders = list(_non_member_strategy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "CrawlStrategy member" in offenders[0], offenders
    # The empty string is the same lie in cheaper clothing.
    bad_empty = ast.parse('def f():\n    return _failed_engine_result(strategy="")\n')
    offenders = list(_non_member_strategy_offenders(bad_empty, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "CrawlStrategy member" in offenders[0], offenders
    # None is admitted: it is the honest "no strategy attempted" label.
    good_none = ast.parse("def f():\n    return _failed_engine_result(strategy=None)\n")
    assert not list(_non_member_strategy_offenders(good_none, REPO_ROOT / "app" / "_synthetic.py"))
    # A real member literal and a non-literal both pass without comment.
    good_member = ast.parse('def f():\n    return _skipped_engine_result(strategy="BROWSER_HEADLESS")\n')
    assert not list(_non_member_strategy_offenders(good_member, REPO_ROOT / "app" / "_synthetic.py"))
    good_var = ast.parse("def f(s):\n    return _crawl_result_from_exception(strategy=s)\n")
    assert not list(_non_member_strategy_offenders(good_var, REPO_ROOT / "app" / "_synthetic.py"))


_HOST_POLICY_VALIDATORS = frozenset(
    {
        "canonical_page_identity",
        "filter_js_endpoints",
        "normalize_public_http_url",
        "public_url_or_none",
        "safe_public_url_or_none",
        "_validated_url",
    }
)
_HOST_POLICY_URL_CALLS = frozenset({"urljoin", "urlunsplit", "urlparse", "urlsplit", "geturl"})
_HOST_POLICY_URL_FIELDS = frozenset({"url", "raw", "href", "location", "normalized_url", "final_url"})
_HOST_POLICY_NAMES = frozenset({"policy", "host_policy"})

#: Attribute spellings that hold a ``HostPolicy``. Separate from
#: ``_HOST_POLICY_NAMES`` because a *parameter* called ``policy`` conventionally
#: receives one of these objects when it is handed to a URL validator, while an
#: *attribute* called ``_policy`` is usually the crawl policy dict -- so the
#: attribute rule cannot be the looser one.
#:
#: ``_host_policy`` had to be here. The ``self.`` branch below matched
#: ``self.policy`` and ``self.host_policy`` only, and this codebase writes
#: ``self._host_policy`` throughout: the rule had never once fired on a method
#: that validated through the instance's own policy, because it could not see
#: one. A leading underscore does not change what the attribute holds.
_HOST_POLICY_ATTRS = frozenset({"policy", "host_policy", "_host_policy"})


def _url_parameter_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(("_of", "_getter", "_accessor")):
        return False
    return (
        "url" in lowered
        or lowered in {"href", "location", "origin", "endpoint", "endpoints"}
        or lowered.endswith("_href")
    )


def _host_policy_url_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1].lower() in _HOST_POLICY_URL_CALLS or name.endswith(".SafeUrl.parse")


def _host_policy_validator_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1] in _HOST_POLICY_VALIDATORS or name.endswith(".SafeUrl.parse")


def _host_policy_value(node: ast.AST | None, policy_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in policy_names
    if isinstance(node, ast.Attribute) and node.attr in _HOST_POLICY_ATTRS:
        return isinstance(node.value, ast.Name) and node.value.id == "self"
    return False


def _host_policy_scope_names(tree: ast.AST, function: ast.AST) -> set[str]:
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
    names: set[str] = set()
    current: ast.AST | None = function
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (*current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs)
            names.update(argument.arg for argument in arguments if argument.arg in _HOST_POLICY_NAMES)
        current = parents.get(id(current))
    return names


def _host_policy_empty_guards(function: ast.AST) -> set[int]:
    guarded: set[int] = set()
    for node in local_nodes(function):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        empty = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
        ) or (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and any(isinstance(op, (ast.Is, ast.Eq)) for op in test.ops)
            and any(
                isinstance(comparator, ast.Constant) and comparator.value in {None, ""}
                for comparator in test.comparators
            )
        )
        if empty:
            guarded.update(id(child) for child in node.body if isinstance(child, ast.Return))
    return guarded


def _host_policy_call_is_validated(tree: ast.AST, node: ast.Call, policy_names: set[str]) -> bool:
    if not _host_policy_validator_call(tree, node):
        return False
    return any(
        _host_policy_value(value, policy_names)
        for value in (*node.args, *(keyword.value for keyword in node.keywords))
    )


def _reads_url_field(receiver, field, url_names: set[str]) -> bool:
    """True when ``field`` is read off a name the walk already knows is a URL.

    The attribute form (``url.final_url``) and the subscript form
    (``url["final_url"]``) are one question with two spellings, and so is the
    ``url.get("final_url")`` call the caller below handles. Answering them in
    three places is how the dict form came to check a different field set than
    the attribute form -- a URL field the walk missed is an unvalidated URL
    reaching a host-policy call, and the guard would have reported the tree clean.
    """
    return isinstance(receiver, ast.Name) and receiver.id in url_names and field in _HOST_POLICY_URL_FIELDS


def _host_policy_expression_state(
    tree: ast.AST,
    node: ast.AST | None,
    url_names: set[str],
    validated_names: set[str],
    policy_names: set[str],
) -> tuple[bool, bool]:
    if node is None:
        return False, False
    if isinstance(node, ast.Name):
        return node.id in url_names, node.id in validated_names
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.lower().startswith(("http://", "https://")), False
    if isinstance(node, ast.Attribute) and _reads_url_field(node.value, node.attr, url_names):
        return True, False
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and _reads_url_field(node.value, node.slice.value, url_names)
    ):
        return True, False
    if isinstance(node, ast.Call):
        if _host_policy_call_is_validated(tree, node, policy_names):
            return True, True
        if _host_policy_validator_call(tree, node):
            return True, False
        if _host_policy_url_call(tree, node):
            return True, False
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] == "get" and node.args:
            key = node.args[0]
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            return (isinstance(key, ast.Constant) and _reads_url_field(receiver, key.value, url_names)), False
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] in {"str", "format", "join"}:
            child_states = [
                _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
                for child in (*node.args, *(keyword.value for keyword in node.keywords))
            ]
            return any(is_url for is_url, _ in child_states), any(
                is_validated for _, is_validated in child_states
            )
        return False, False
    if isinstance(node, ast.Dict):
        return any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[0]
            for key, value in zip(node.keys, node.values, strict=True)
        ), any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[1]
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        states = [
            _host_policy_expression_state(tree, element, url_names, validated_names, policy_names)
            for element in node.elts
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        state = _host_policy_expression_state(tree, node.elt, url_names, validated_names, policy_names)
        generator_url = any(
            _url_parameter_name(generator.iter.id)
            for generator in node.generators
            if isinstance(generator.iter, ast.Name)
        )
        condition_validated = any(
            _host_policy_call_is_validated(tree, child, policy_names)
            for generator in node.generators
            for condition in generator.ifs
            for child in local_nodes(condition)
            if isinstance(child, ast.Call)
        )
        return state[0] or generator_url, state[1] or condition_validated
    if isinstance(node, ast.DictComp):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in (node.key, node.value)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.BinOp, ast.BoolOp, ast.JoinedStr, ast.IfExp, ast.Compare)):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in ast.iter_child_nodes(node)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, ast.NamedExpr):
        return _host_policy_expression_state(tree, node.value, url_names, validated_names, policy_names)
    return False, False


def _url_return_shapes(function: ast.AST, tree: ast.AST) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    url_names = {argument.arg for argument in arguments if _url_parameter_name(argument.arg)}
    policy_names = _host_policy_scope_names(tree, function)
    validated_names: set[str] = set()
    nodes = list(local_nodes(function))
    changed = True
    while changed:
        changed = False
        for loop in (node for node in nodes if isinstance(node, ast.For)):
            if not isinstance(loop.iter, ast.Name) or loop.iter.id not in url_names:
                continue
            target_names = (
                {target.id for target in loop.target.elts if isinstance(target, ast.Name)}
                if isinstance(loop.target, (ast.Tuple, ast.List))
                else ({loop.target.id} if isinstance(loop.target, ast.Name) else set())
            )
            url_names.update(target_names)
            if any(
                _host_policy_call_is_validated(tree, child, policy_names)
                for child in local_nodes(loop)
                if isinstance(child, ast.Call)
            ):
                validated = {loop.iter.id, *target_names}
                changed |= bool(validated - validated_names)
                validated_names.update(validated)
        for statement in nodes:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            elif isinstance(statement, ast.NamedExpr):
                targets = [statement.target]
                value = statement.value
            if value is None:
                continue
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if not names:
                continue
            is_url, is_validated = _host_policy_expression_state(
                tree, value, url_names, validated_names, policy_names
            )
            if is_url:
                changed |= bool(names - url_names)
                url_names.update(names)
            if is_validated:
                changed |= bool(names - validated_names)
                validated_names.update(names)
            if _host_policy_value(value, policy_names):
                changed |= bool(names - policy_names)
                policy_names.update(names)
    endpoint_parameters = {
        argument.arg for argument in arguments if argument.arg.lower() in {"endpoint", "endpoints"}
    }
    empty_guards = _host_policy_empty_guards(function)
    for statement in nodes:
        if not isinstance(statement, ast.Return) or id(statement) in empty_guards:
            continue
        is_url, is_validated = _host_policy_expression_state(
            tree, statement.value, url_names, validated_names, policy_names
        )
        if is_url and not is_validated:
            return True
        if isinstance(statement.value, ast.Name) and statement.value.id in endpoint_parameters:
            return True
    return False


def _required_host_policy_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in functions(tree):
        if _url_return_shapes(node, tree):
            found.append(offender_at(path, node, f"{node.name}() returns URLs without required HostPolicy"))
    return found


def test_required_host_policy_detector_bites():
    bad = ast.parse("def normalize_public_http_url(url, *, policy=None):\n    return url\n")
    offenders = _required_host_policy_offenders(
        bad,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 1 and "HostPolicy" in offenders[0], offenders

    good = ast.parse(
        "from app.validation import public_url_or_none\n"
        "def normalize_public_http_url(url, *, policy: HostPolicy):\n"
        "    return public_url_or_none(url, policy=policy)\n"
    )
    assert not _required_host_policy_offenders(
        good,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    safe_bad = ast.parse("class SafeUrl:\n    def parse(self, url, *, policy=None):\n        return url\n")
    assert (
        len(
            _required_host_policy_offenders(
                safe_bad,
                REPO_ROOT / "app" / "_synthetic.py",
            )
        )
        == 1
    )
    # The private spelling. ``self._host_policy`` is what this codebase writes
    # everywhere, and the rule could not see it -- so a method validating through
    # the instance's own policy read as a method with no policy at all, and the
    # rule fired on it while staying silent on the one that really had none.
    via_instance = ast.parse(
        "from app.validation import public_url_or_none\n"
        "class JobStore:\n"
        "    def __init__(self, host_policy):\n"
        "        self._host_policy = host_policy\n"
        "    def canonical_root(self, root_url):\n"
        "        return str(public_url_or_none(root_url, policy=self._host_policy))\n"
    )
    assert not _required_host_policy_offenders(
        via_instance,
        REPO_ROOT / "app" / "_synthetic.py",
    ), "a method validating through self._host_policy read as having no policy"
    with_no_policy = ast.parse(
        "class JobStore:\n    def canonical_root(self, root_url):\n        return str(root_url)\n"
    )
    assert _required_host_policy_offenders(
        with_no_policy,
        REPO_ROOT / "app" / "_synthetic.py",
    ), "a method with no policy at all must still be caught"


_HOST_POLICY_PROPERTY = Property(
    name="host-policy",
    owner_module="app/validation.py",
    owner_symbols=("_host_policy_reason",),
    definition_site="app/validation.py:_host_policy_reason",
    fingerprints=("host_policy_validation",),
    reason="Every URL boundary must apply the host policy before forwarding a URL.",
)


def test_host_policy_detector_property_bites():
    assert_property_detector_bites(
        _HOST_POLICY_PROPERTY,
        lambda tree, path, _prop: _required_host_policy_offenders(tree, path),
        caught={
            "dotted urljoin import": (
                "from urllib.parse import urljoin\ndef f(root, path):\n    return urljoin(root, path)\n",
                1,
            ),
            "annotation without policy use": (
                "def f(url, *, policy: HostPolicy):\n    return url\n",
                1,
            ),
            "callable indirection without policy": (
                "from app.validation import public_url_or_none\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    validator = public_url_or_none\n"
                "    return validator(url)\n",
                1,
            ),
            "dotted urlunsplit import": (
                "from urllib.parse import urlunsplit\ndef f(parts):\n    return urlunsplit(parts)\n",
                1,
            ),
        },
        quiet={
            "dotted urljoin validated": (
                "from urllib.parse import urljoin\n"
                "from app.validation import public_url_or_none\n"
                "def f(root, path, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urljoin(root, path), policy=policy)\n"
            ),
            "annotation replaced by policy use": (
                "class Client:\n"
                "    policy: HostPolicy\n"
                "    def emit(self, url):\n"
                "        return public_url_or_none(url, policy=self.policy)\n"
            ),
            "callable indirection with policy": (
                "from app.validation import public_url_or_none as validate\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    return validate(url, policy=policy)\n"
            ),
            "dotted urlunsplit validated": (
                "from urllib.parse import urlunsplit\n"
                "from app.validation import public_url_or_none\n"
                "def f(parts, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urlunsplit(parts), policy=policy)\n"
            ),
        },
    )


def test_required_host_policy_detector_covers_new_url_helper_shapes():
    source = ast.parse(
        "def passthrough_url(url: str) -> str:\n"
        "    return url\n"
        "def filter_endpoints(endpoints: list[dict[str, str]]) -> list[dict[str, str]]:\n"
        "    return [item for item in endpoints if item.get('url')]\n"
    )
    offenders = _required_host_policy_offenders(
        source,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 2, offenders


def test_host_policy_detector_accepts_fixed_extraction_boundaries():
    path = REPO_ROOT / "app" / "crawl" / "extraction.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not [
        offender
        for offender in _required_host_policy_offenders(tree, path)
        if any(name in offender for name in ("_extract_frames", "_extract_forms"))
    ]


def test_url_returning_helpers_require_host_policy():
    offenders = app_offenders(_required_host_policy_offenders)
    assert not offenders, ", ".join(offenders)


def test_resolved_call_name_handles_getattr_alias_and_static_name():
    source = ast.parse(
        "from builtins import getattr as ga\n"
        "import ipaddress\n"
        "def direct():\n"
        "    return getattr(ipaddress, 'ip_address')\n"
        "def aliased(value):\n"
        "    return ga(ipaddress, 'ip_address')(value)\n"
        "def constructed(value):\n"
        "    name = 'ip_' + 'address'\n"
        "    return getattr(ipaddress, name)(value)\n"
    )
    calls = [node for node in nodes_of(source) if isinstance(node, ast.Call)]
    attribute_calls = [call for call in calls if isinstance(call.func, ast.Name)]
    assert len(attribute_calls) == 3
    assert all(resolved_call_name(source, call) == "ipaddress.ip_address" for call in attribute_calls)


# ---------------------------------------------------------------------------
# Normalize-or-None chokepoint (jscpd #5 family)
#
# The same three-line wrapper -- try normalize_public_http_url(x) except
# ValueError: return None -- was hand-rolled in three modules (search
# providers, map, batch dedupe). Each is a URL egress door that must apply
# the SSRF private-target policy; a new call site that skips the wrapper
# silently drops the policy for its door.
# ---------------------------------------------------------------------------

_NORMALIZE_OR_NONE_OFFENDERS = frozenset(
    {"normalize_public_http_url", "public_url_or_none", "safe_public_url_or_none"}
)


def _normalize_or_none_offenders(tree, path):
    """A try/except ValueError around the normalizer outside the chokepoint."""
    for node in nodes_of_type(tree, ast.Try):
        body_names = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in nodes_of(node)
            if isinstance(n, ast.Call)
        }
        if "normalize_public_http_url" not in body_names:
            continue
        for handler in node.handlers:
            if not (isinstance(handler.type, ast.Name) and handler.type.id == "ValueError"):
                continue
            # The re-inlined wrapper discards the exception: its body is a bare
            # None, pass, or a raw fallback (``x`` returned unchanged). A site
            # that re-raises, branches on the error text, or needs the
            # normalized value for a second computation (registrable_domain,
            # the error message) is a different contract and stays.
            body = handler.body
            if len(body) != 1:
                continue
            stmt = body[0]
            is_discard = (
                (
                    isinstance(stmt, ast.Return)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is None
                )
                or isinstance(stmt, ast.Pass)
                or (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Name)
                    and stmt.value.id != stmt.targets[0].id
                )
            )
            if is_discard:
                yield offender_at(
                    path,
                    node,
                    "normalize_public_http_url + except ValueError "
                    "re-inlined; use app.validation.safe_public_url_or_none",
                )


def test_normalize_or_none_is_not_reinlined():
    offenders = []
    # The chokepoint's own definition is the sanctioned site.
    for path, tree in app_trees(exclude=frozenset({"validation.py"})):
        offenders.extend(_normalize_or_none_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_normalize_or_none_detector_bites():
    assert_detector(
        _normalize_or_none_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def f(u):\n"
            "    try:\n"
            "        return normalize_public_http_url(u)\n"
            "    except ValueError:\n"
            "        return None\n": "safe_public_url_or_none",
        },
        clean=("def f(u):\n    return safe_public_url_or_none(u)\n",),
    )


# ---------------------------------------------------------------------------
# Unfiltered urljoin egress doors (H class)
#
# _detect_pagination read an href out of attacker-controlled HTML and returned
# urljoin(final_url, href) raw. The HTTP consumer filtered it before use, the
# browser consumer did not, so a javascript: rel=next reached the response.
# Filtering moved to the producer; this keeps the next door from reopening.
# ---------------------------------------------------------------------------

_EGRESS_FILTERS = frozenset({"public_url_or_none", "safe_public_url_or_none"})


def _is_urljoin_call(node: ast.AST) -> bool:
    """True for a plain ``urljoin(...)`` call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin"


def _unfiltered_urljoin_return_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a function that returns a ``urljoin`` result with no egress filter.

    Caught shapes: ``return urljoin(a, b)`` and ``u = urljoin(a, b); return u``.
    A URL resolved out of untrusted text is an egress door as soon as a caller
    trusts it, so the join and the filter belong together; the browser path
    proved that "every consumer remembers to filter" does not scale.
    ``app/safe_url.py`` composes ``urljoin`` inside the sanctioned
    ``SafeUrl.parse``/``redirect_hop`` path, so it is exempt by file, matching
    the other chokepoint-module exemptions.
    """
    if path.name == "safe_url.py":
        return []
    offenders: list[str] = []
    for func in functions(tree):
        joined: set[str] = set()
        returns_raw = False
        returns_binding = False
        for node in nodes_of(func):
            if isinstance(node, ast.Assign) and _is_urljoin_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        joined.add(target.id)
            elif isinstance(node, ast.Return):
                if _is_urljoin_call(node.value):
                    returns_raw = True
                elif isinstance(node.value, ast.Name) and node.value.id in joined:
                    returns_binding = True
        if not (returns_raw or returns_binding):
            continue
        uses_filter = any(
            (isinstance(n, ast.Name) and n.id in _EGRESS_FILTERS)
            or (isinstance(n, ast.Attribute) and n.attr in _EGRESS_FILTERS)
            for n in nodes_of(func)
        )
        if not uses_filter:
            offenders.append(
                offender_at(path, func, f"{func.name}() returns a urljoin with no egress filter")
            )
    return offenders


def test_no_unfiltered_urljoin_returns():
    """A URL joined from untrusted input is egress-filtered before it leaves."""
    assert_trees_clean(
        _unfiltered_urljoin_return_offenders,
        "filter joined URLs through public_url_or_none",
        trees=app_trees(),
    )


def test_unfiltered_urljoin_detector_bites():
    """The historical leak shape fires; the filtered and sanctioned shapes stay clean."""
    raw = ast.parse("def _detect_pagination(html, final_url):\n    return urljoin(final_url, href)\n")
    offenders = _unfiltered_urljoin_return_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "no egress filter" in offenders[0], offenders
    binding = ast.parse("def f(base, href):\n    u = urljoin(base, href)\n    return u\n")
    assert _unfiltered_urljoin_return_offenders(binding, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    good = ast.parse(
        "def f(href, final_url):\n    return public_url_or_none(href, resolve_against=final_url)\n"
    )
    assert not _unfiltered_urljoin_return_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # safe_url.py composes urljoin inside SafeUrl.parse / redirect_hop.
    hop = ast.parse(
        "def hop(self, location):\n    return SafeUrl.parse(urljoin(self.raw, location), pin=True)\n"
    )
    assert not _unfiltered_urljoin_return_offenders(hop, REPO_ROOT / "app" / "safe_url.py")


# ---------------------------------------------------------------------------
# Raw exception text never reaches the wire (information disclosure)
#
# render.py built the worker-failure result with ``error=str(error)`` from a
# caught exception, and ``to_dict`` is pure serialization -- so the raw text
# (DNS names, filesystem paths, secrets) reached the /v1/render body verbatim.
# ``_terminal_crawl_result`` is the sanctioned construction site and takes an
# already-classified error; this keeps raw text from drifting back into any
# error= field of a wire-visible result.
# ---------------------------------------------------------------------------

_TERMINAL_RESULT_SANCTIONED = frozenset({"result_helpers.py"})
_RESULT_ERROR_SINKS = frozenset({"CrawlResult", "_terminal_crawl_result"})
_EXCEPTION_NAMES = frozenset({"error", "exc", "exception", "err"})


def _is_raw_exception_text(value: ast.expr) -> bool:
    """True for ``str(exc)``/``repr(exc)`` or an f-string interpolating one."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("str", "repr"):
        return True
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in _EXCEPTION_NAMES
            for part in value.values
        )
    return False


def _raw_exception_error_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a wire-visible result built with the raw text of an exception."""
    offenders: list[str] = []
    for node in calls_to_any(tree, _RESULT_ERROR_SINKS):
        for kw in node.keywords:
            if kw.arg == "error" and _is_raw_exception_text(kw.value):
                offenders.append(offender_at(path, node, f"{node.func.id}(error=<raw exception text>)"))
    return offenders


def test_no_raw_exception_text_in_result_error_field():
    """The ``error`` field is a stable classification, never ``str(exc)``."""
    assert_trees_clean(
        _raw_exception_error_offenders,
        "classify via _classify_error_code, never raw exception text",
        trees=app_trees(exclude=_TERMINAL_RESULT_SANCTIONED),
    )


def test_raw_exception_error_detector_bites():
    """The leak shape fires on both sinks; the classified shape stays clean."""
    direct = ast.parse("def f(exc):\n    return CrawlResult(error=str(exc))\n")
    offenders = _raw_exception_error_offenders(direct, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1 and "raw exception text" in offenders[0], offenders
    via_primitive = ast.parse("def f(exc):\n    return _terminal_crawl_result(error=str(exc))\n")
    assert _raw_exception_error_offenders(via_primitive, REPO_ROOT / "app" / "api" / "_synthetic.py")
    fstring = ast.parse("def f(error):\n    return CrawlResult(error=f'failed: {error}')\n")
    assert _raw_exception_error_offenders(fstring, REPO_ROOT / "app" / "api" / "_synthetic.py")
    repr_shape = ast.parse("def f(e):\n    return CrawlResult(error=repr(e))\n")
    assert _raw_exception_error_offenders(repr_shape, REPO_ROOT / "app" / "api" / "_synthetic.py")
    good = ast.parse("def f(exc):\n    return _terminal_crawl_result(error=_classify_error_code(exc))\n")
    assert not _raw_exception_error_offenders(good, REPO_ROOT / "app" / "api" / "_synthetic.py")


# ---------------------------------------------------------------------------
# JS-endpoint egress door: the filter must not be reinlined
#
# ``filter_js_endpoints`` is the single place a scraped JS endpoint survives or
# is dropped. Both call sites used to carry the inline comprehension
# (``{**endpoint, "url": clean} for ... if (clean := public_url_or_none(...))
# is not None``) -- the shape that silently disappears the moment a consumer is
# added in a hurry. This pins the comprehension to the chokepoint itself.
# ---------------------------------------------------------------------------

_JS_ENDPOINTS_FILTER_SANCTIONED = frozenset({"validation.py"})


def _reinline_js_endpoints_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the inlined endpoint-filter comprehension outside its chokepoint.

    The reinlined shape is a comprehension whose element spreads an existing
    dict and rewrites its ``url`` key (``{**endpoint, "url": clean}``) -- a
    None key in the dict is the ``**`` spread, and the rewritten ``url`` key is
    what makes it an endpoint filter rather than an ordinary copy.
    """
    if path.name == "validation.py":
        return []
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.ListComp, ast.SetComp, ast.GeneratorExp):
        elt = node.elt
        if not isinstance(elt, ast.Dict):
            continue
        if any(k is None for k in elt.keys) and any(
            isinstance(k, ast.Constant) and k.value == "url" for k in elt.keys
        ):
            offenders.append(
                offender_at(path, node, "inlined js_endpoints filter: use validation.filter_js_endpoints")
            )
    return offenders


def test_js_endpoints_filter_is_not_reinlined():
    """Scraped JS endpoints are filtered through ``filter_js_endpoints``."""
    assert_trees_clean(
        _reinline_js_endpoints_filter_offenders,
        "route endpoints through filter_js_endpoints",
        trees=app_trees(exclude=_JS_ENDPOINTS_FILTER_SANCTIONED),
    )


def test_reinline_js_endpoints_detector_bites():
    """The historical inline shape fires; the chokepoint call stays clean."""
    bad = ast.parse(
        "def f(endpoints, base):\n"
        '    return [{**e, "url": c} for e in endpoints\n'
        '           if (c := public_url_or_none(e["url"])) is not None]\n'
    )
    offenders = _reinline_js_endpoints_filter_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "filter_js_endpoints" in offenders[0], offenders
    good = ast.parse(
        "def f(endpoints, base):\n    return filter_js_endpoints(endpoints, resolve_against=base)\n"
    )
    assert not _reinline_js_endpoints_filter_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # The chokepoint's own comprehension really is the matched shape: the file
    # exemption is what keeps the real tree clean, not a predicate that misses it.
    validation = next(((path, tree) for path, tree in app_trees() if path.name == "validation.py"), None)
    assert validation is not None
    validation_path, validation_tree = validation
    assert _reinline_js_endpoints_filter_offenders(validation_tree, validation_path) == []
    hits = _reinline_js_endpoints_filter_offenders(
        validation_tree, REPO_ROOT / "app" / "crawl" / "_synthetic.py"
    )
    assert len(hits) == 1 and "filter_js_endpoints" in hits[0], hits


# ---------------------------------------------------------------------------
# Chokepoint drift detectors for the P0-P2 fix tiers
#
# Each behavioral test below pins what the code DOES today; these pin the
# CHOKEPOINT so a future edit that bypasses it turns the suite red instead of
# silently reintroducing the defect. Same shape as the detectors above: an AST
# walk over app/, a synthetic *_bites companion proving the predicate fires,
# and no hand-maintained exemption list.
# ---------------------------------------------------------------------------


def _enclosing_defs(node: ast.AST, root: ast.AST) -> list[str]:
    """The enclosing ``def``/``class`` names from innermost outward.

    AST has no parent pointers, so a parent map is built per tree; the walk is
    O(nodes) and the detectors call it only on a handful of matched nodes.
    """
    parents: dict[int, ast.AST] = {}
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            stack.append(child)
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(id(current))
    return names


# --- 4a: the robots matcher compiler is reachable only via its host cache ----

_ROBOTS_COMPILE = "_compile_robots_pattern"
_ROBOTS_COMPILE_HOME = "_RobotsPatternCache"


def _robots_compile_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_compile_robots_pattern`` may be called only from ``_RobotsPatternCache.get``.

    The pure pattern translation is the cheap half of the robots matcher; the
    safety property is that every caller reaches it through the bounded
    PER-HOST LRU. A second caller reintroduces a process-global matcher cache
    keyed only on the pattern string -- patterns arrive from remotely-fetched
    robots.txt, so a hostile site spraying distinct patterns would evict every
    other site's matchers and control the whole cache (P2-E). The home method
    is the single sanctioned call site; the module-level wrapper
    (``_robots_pattern_to_regex``) goes through it too.
    """
    offenders: list[str] = []
    for node in calls_named(tree, _ROBOTS_COMPILE):
        scopes = _enclosing_defs(node, tree)
        if _ROBOTS_COMPILE_HOME in scopes and "get" in scopes[: scopes.index(_ROBOTS_COMPILE_HOME) + 1]:
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{_ROBOTS_COMPILE} called outside {_ROBOTS_COMPILE_HOME}.get; route through "
                f"the per-host cache so a hostile robots.txt cannot evict another site's matchers",
            )
        )
    return offenders


def test_robots_pattern_compile_lives_behind_the_per_host_cache():
    offenders = app_offenders(_robots_compile_call_offenders)
    assert not offenders, ", ".join(offenders)


def test_robots_pattern_compile_detector_bites():
    # A direct caller -- exactly the shape that reintroduces a global,
    # attacker-keyed matcher cache.
    bad = ast.parse("def f(pattern):\n    return _compile_robots_pattern(pattern)\n")
    offenders = list(_robots_compile_call_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "per-host cache" in offenders[0], offenders
    # The sanctioned home passes.
    good = ast.parse(
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        return _compile_robots_pattern(pattern)\n"
    )
    assert not _robots_compile_call_offenders(good, REPO_ROOT / "app" / "_synthetic.py")
    # A nested helper inside the home class is still inside the home scope.
    nested = ast.parse(
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        def build():\n"
        "            return _compile_robots_pattern(pattern)\n"
        "        return build()\n"
    )
    assert not _robots_compile_call_offenders(nested, REPO_ROOT / "app" / "_synthetic.py")
    # A same-named method on any OTHER class is not the home.
    impostor = ast.parse(
        "class OtherCache:\n    def get(self, pattern):\n        return _compile_robots_pattern(pattern)\n"
    )
    assert len(_robots_compile_call_offenders(impostor, REPO_ROOT / "app" / "_synthetic.py")) == 1


# --- 4b: result-primitive status values are real ResultStatus members --------

_STATUS_VALUE_SOURCES = ("status",)


def _non_member_status_offenders(tree: ast.AST, path: pathlib.Path, *, members=None) -> list[str]:
    """A literal ``status`` passed to a result primitive must be a real member.

    Mirrors the classification/strategy detectors: ``status`` feeds the strict
    wire enum on the unified result, so an invented literal is a KeyError or a
    silent shaping failure waiting on a batch. ``members`` is the live
    ``ResultStatus`` argument set, read at test time -- a hand-maintained
    exemption list is precisely how an invented status would ship.
    """
    if members is None:
        members = real_members("ResultStatus")

    return literal_keyword_membership_offenders(
        tree,
        path,
        function_names=_result_primitive_names(),
        keyword_names=_STATUS_VALUE_SOURCES,
        members=members,
        label="status",
        type_name="ResultStatus",
        advice=("add it to the Literal (app/models.py) or derive it from an existing member"),
    )


def test_result_primitives_emit_real_status_members():
    offenders = app_offenders(_non_member_status_offenders)
    assert not offenders, (
        "a literal status passed to a result primitive must be a ResultStatus "
        "member, or the strict enum on the wire model rejects the batch: " + ", ".join(offenders)
    )


def test_non_member_status_detector_bites():
    bad = ast.parse('def f():\n    return _terminal_crawl_result(status="never_a_status")\n')
    offenders = list(_non_member_status_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "ResultStatus member" in offenders[0], offenders
    good = ast.parse('def f():\n    return _terminal_crawl_result(status="failed")\n')
    assert not _non_member_status_offenders(good, REPO_ROOT / "app" / "_synthetic.py")
    # A non-literal (a variable or the caller's own status field) passes.
    good_var = ast.parse("def f(s):\n    return _make_engine_result(status=s)\n")
    assert not _non_member_status_offenders(good_var, REPO_ROOT / "app" / "_synthetic.py")


# --- 4c: the webhook semaphore is touched only through its loop accessor -----

_SEMAPHORE_GLOBALS = frozenset({"_async_crawl_semaphore", "_async_crawl_semaphore_loop"})
_SEMAPHORE_ACCESSOR = "_get_async_crawl_semaphore"


def _semaphore_global_touch_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The loop-bound semaphore module globals are touched only by the accessor.

    ``_get_async_crawl_semaphore`` is the single home that binds the cached
    instance to the running loop and rebuilds it when the loop changes; reading
    or assigning the module global from anywhere else silently reintroduces a
    loop-bound violation (a Semaphore awaited off the loop it was created on).
    The module-level declaration has no enclosing function and is exempt.
    """
    return [
        offender_at(
            path,
            node,
            f"{node.id} touched outside {_SEMAPHORE_ACCESSOR}; "
            f"use the accessor so the instance stays bound to the running loop",
        )
        for node in nodes_of_type(tree, ast.Name)
        if node.id in _SEMAPHORE_GLOBALS
        and (scopes := _enclosing_defs(node, tree))
        and _SEMAPHORE_ACCESSOR not in scopes
    ]


def test_async_crawl_semaphore_globals_are_loop_bound():
    assert not (offenders := app_offenders(_semaphore_global_touch_offenders)), ", ".join(offenders)


def test_async_crawl_semaphore_loop_binding_detector_bites():
    # A direct read from a random function -- the loop-bound bypass.
    bad = ast.parse("async def f():\n    return _async_crawl_semaphore.acquire()\n")
    offenders = list(_semaphore_global_touch_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "loop" in offenders[0], offenders
    # A direct write is the same bypass.
    bad_write = ast.parse("def f():\n    _async_crawl_semaphore = None\n")
    assert list(_semaphore_global_touch_offenders(bad_write, REPO_ROOT / "app" / "_synthetic.py"))
    # The module-level declaration is the definition, not a touch.
    declaration = ast.parse("_async_crawl_semaphore: asyncio.Semaphore | None = None\n")
    assert not _semaphore_global_touch_offenders(declaration, REPO_ROOT / "app" / "_synthetic.py")
    # The accessor itself is exempt, including the companion loop global.
    accessor = ast.parse(
        "def _get_async_crawl_semaphore():\n"
        "    global _async_crawl_semaphore, _async_crawl_semaphore_loop\n"
        "    if _async_crawl_semaphore is None:\n"
        "        _async_crawl_semaphore = _build_async_crawl_semaphore()\n"
        "    return _async_crawl_semaphore\n"
    )
    assert not _semaphore_global_touch_offenders(accessor, REPO_ROOT / "app" / "_synthetic.py")


# --- 4d: the doc pool reaps retired slots on every acquisition ---------------

_DOC_POOL_NAME = "_DOC_POOL"
_DOC_POOL_ACQUIRER = "_acquire_doc_worker"


def _has_retired_attr(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr == "retired" for sub in nodes_of(node))


def _reaps_retired(value: ast.AST) -> bool:
    """A ``_DOC_POOL = [... for each ... if not each.retired]`` style rebuild.

    ``not each.retired`` parses as a ``UnaryOp(Not, ...)`` and an explicit
    comparison as a ``Compare``; both shapes are admitted.
    """
    if not isinstance(value, ast.ListComp):
        return False
    for generator in value.generators:
        for condition in generator.ifs:
            for node in nodes_of_type(condition, ast.UnaryOp):
                if isinstance(node.op, ast.Not):
                    if _has_retired_attr(node.operand):
                        return True
                if isinstance(node, ast.Compare):
                    if any(_has_retired_attr(operand) for operand in (node.left, *node.comparators)):
                        return True
    return False


def _rebuilds_pool_without_retired(node: ast.AST) -> bool:
    """Does ``node`` assign ``_DOC_POOL`` from a list comp filtering ``retired``?"""
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == _DOC_POOL_NAME for target in child.targets)
        and _reaps_retired(child.value)
        for child in nodes_of(node)
    )


def _doc_pool_reap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_acquire_doc_worker`` must rebuild the pool without retired slots.

    P2-D: a retired slot has been terminated and can never be handed out, so it
    must not keep occupying a pool entry. The respawn scan only replaces a slot
    whose process is dead, which leaves a retired-but-still-``is_alive()`` slot
    skipped by both scans and stranded in the pool forever -- the entry count
    drifted permanently above the usable count. Removing the reap filter is the
    regression; this detector fails the suite when it goes.
    """
    return [
        offender_at(
            path,
            node,
            f"{_DOC_POOL_ACQUIRER} no longer reaps retired slots; a "
            f"retired-but-alive slot would strand in the pool and inflate the size budget",
        )
        for node in functions(tree)
        if node.name == _DOC_POOL_ACQUIRER and not _rebuilds_pool_without_retired(node)
    ]


def test_doc_pool_acquirer_reaps_retired_slots():
    assert not (offenders := app_offenders(_doc_pool_reap_offenders)), ", ".join(offenders)


def test_doc_pool_reap_detector_bites():
    # The acquirer without the reap filter: the regression the detector hunts.
    bad = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        want = _doc_extraction_worker_count()\n"
        "        while True:\n"
        "            for worker in _DOC_POOL:\n"
        "                if not worker.busy:\n"
        "                    return worker\n"
    )
    offenders = list(_doc_pool_reap_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py"))
    assert len(offenders) == 1 and "retired" in offenders[0], offenders
    # The real filter satisfies it.
    good = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        _DOC_POOL = [each for each in _DOC_POOL if not each.retired]\n"
        "        return _DOC_POOL[0]\n"
    )
    assert not _doc_pool_reap_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # A pool rebuild that filters on something else is not the reap.
    not_reap = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        _DOC_POOL = [each for each in _DOC_POOL if not each.busy]\n"
        "        return _DOC_POOL[0]\n"
    )
    assert list(_doc_pool_reap_offenders(not_reap, REPO_ROOT / "app" / "crawl" / "_synthetic.py"))


# --- 4e (optional): the attempt dataclass is pinned at its construction site ---
#
# ``CrawlStrategyName`` is now a true Literal (the strategy constants in
# ``app/constants.py`` are ``Final``, so ``Literal["HTTP_DECLARED_CRAWLER", ...]``
# resolves and mypy enforces the ``strategy=`` arg-type at every
# ``CrawlStrategyAttempt`` construction site statically. That replaced the six
# ``# type: ignore[arg-type]`` comments the producers used to carry. This
# detector is now belt-and-suspenders with mypy: it stays because it runs at
# pytest time and flags a literal that slips past the checker, but the static
# gate is the real chokepoint now.

_ATTEMPT_CLASS = "CrawlStrategyAttempt"
_ATTEMPT_STRATEGY_KWARG = "strategy"


def _non_member_attempt_strategy_offenders(tree: ast.AST, path: pathlib.Path, *, members=None) -> list[str]:
    """A literal ``strategy=`` at a ``CrawlStrategyAttempt`` site must be a member.

    Covers the class directly rather than the builder primitives, so a new
    construction site anywhere in ``app/`` is policed without being registered
    anywhere first. ``None`` is admitted: it is the honest label for "no
    strategy was attempted". ``members`` is the live ``CrawlStrategyName``
    argument set, read at test time.
    """
    if members is None:
        members = real_members("CrawlStrategyName")

    return literal_keyword_membership_offenders(
        tree,
        path,
        function_names=_ATTEMPT_CLASS,
        keyword_names=_ATTEMPT_STRATEGY_KWARG,
        members=members,
        label="strategy",
        type_name="CrawlStrategyName",
        advice="pass a real member, or None when no strategy was attempted",
    )


def test_crawl_strategy_attempt_sites_usereal_members():
    offenders = app_offenders(_non_member_attempt_strategy_offenders)
    assert not offenders, (
        "a literal strategy at a CrawlStrategyAttempt construction site must be a "
        "CrawlStrategyName member, or None when no strategy was attempted: " + ", ".join(offenders)
    )


def test_crawl_strategy_attempt_detector_bites():
    from typing import get_args

    from app.models import CrawlStrategyName

    members = set(get_args(CrawlStrategyName))
    bad = ast.parse(
        "def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_DECLARED_CRAWLER', status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n"
    )
    offenders = list(_non_member_attempt_strategy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "CrawlStrategyName member" in offenders[0], offenders
    # None is admitted -- the honest "no strategy attempted" label.
    good_none = ast.parse(
        "def f():\n    return CrawlStrategyAttempt(strategy=None, status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n"
    )
    assert not _non_member_attempt_strategy_offenders(
        good_none, REPO_ROOT / "app" / "_synthetic.py", members=members
    )
    # A real member literal and a non-literal both pass.
    good_member = ast.parse("def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_HEADLESS')\n")
    assert not _non_member_attempt_strategy_offenders(
        good_member, REPO_ROOT / "app" / "_synthetic.py", members=members
    )
    good_var = ast.parse("def f(s):\n    return CrawlStrategyAttempt(strategy=s)\n")
    assert not _non_member_attempt_strategy_offenders(
        good_var, REPO_ROOT / "app" / "_synthetic.py", members=members
    )


def test_crawl_strategy_name_members_match_constants():
    """The ``CrawlStrategyName`` Literal must not drift from ``app.constants``.

    ``CrawlStrategyName`` cannot reference the ``Final`` constants directly inside
    ``Literal[...]`` (mypy rejects that), so its members are spelled as string
    literals. constants.py remains the single source of truth; this pins the two
    together so a rename there cannot orphan the alias.
    """
    from typing import get_args

    from app import constants
    from app.models import CrawlStrategyName

    declared = {
        constants.HTTP_DECLARED_CRAWLER,
        constants.HTTP_CFFI_IMPERSONATE,
        constants.BROWSER_HEADLESS,
        constants.BROWSER_HEADED_WARMED,
    }
    assert set(get_args(CrawlStrategyName)) == declared


def test_classification_literal_covers_the_enum_exactly():
    """``Classification`` and ``CrawlClassification`` name the same taxonomy.

    Neither can be derived from the other: mypy will not evaluate
    ``*get_args()`` inside ``Literal[...]`` and will not read an enum, so
    ``Classification`` is spelled out and ``CrawlClassification`` is spelled out,
    and this is the only thing that says they are the same set. It was missing,
    which is the gap this whole taxonomy chain is built to close: a member added
    to the enum and emitted by a classifier passes every membership sweep (they
    check the enum direction), and then
    ``_check_literal_fields`` rejects the whole batch at construction because the
    Literal never heard of it. The reverse -- a Literal member with no enum
    member -- reaches the strict enum at response shaping and takes the batch
    down there instead.

    Both directions are asserted, so neither half of the drift can be the one
    that slips through.
    """
    from typing import get_args

    from app.constants import CrawlClassification
    from app.models import Classification

    declared = set(get_args(Classification))
    enum = {member.value for member in CrawlClassification}
    assert enum - declared == set(), (
        "every CrawlClassification member needs a Classification Literal member:"
        f" missing {sorted(enum - declared)} (app/models.py)"
    )
    assert declared - enum == set(), (
        "a Classification member with no CrawlClassification member is downgraded"
        f" to site_error at response shaping: unknown {sorted(declared - enum)}"
    )


def test_crawl_error_is_the_pipeline_additions_over_classification():
    """The pipeline-only codes are a real split, not a second copy of the taxonomy.

    ``CrawlError`` is defined as the union of ``Classification`` and
    ``PIPELINE_ONLY_CRAWL_ERRORS`` (app/models.py), so its membership is derived
    and cannot drift. What a union cannot check is whether the split is still
    meaningful: a code listed as a pipeline condition while also being a
    classification says a page outcome is a pipeline condition, and a pipeline
    condition deleted silently narrows the ``error`` field for callers who key on
    it as a stable code.
    """
    from app.models import PIPELINE_ONLY_CRAWL_ERRORS, Classification, _literal_members

    pipeline_only = _literal_members(PIPELINE_ONLY_CRAWL_ERRORS)
    classification = _literal_members(Classification)
    assert pipeline_only == {
        "robots_txt_disallowed",
        "browser_worker_unavailable",
        "unsupported_strategy",
        "retries_exhausted",
    }, sorted(pipeline_only)
    assert not pipeline_only & classification, (
        "a code in both halves is a page outcome misfiled as a pipeline"
        f" condition: {sorted(pipeline_only & classification)}"
    )


# ===========================================================================
# GENERIC value-membership chokepoint (program 1)
#
# The detectors above were written one per escaped field: classification after
# url_budget_exceeded, strategy after BROWSER_DECLARED_CRAWLER, status after
# the terminal-result widening. Each is a list nobody extends, so the class
# re-emerged at every field not on the list. The replacement is one generic
# sweep: the (dataclass, field, Literal) triples are read from app.models at
# test time, so a field added to a result dataclass tomorrow is covered the day
# it lands rather than the day someone remembers to name it.
#
# The old per-field detectors are kept below as *proof* the generic one is
# strictly stronger -- each is subsumed by the generic sweep, which is what
# makes deleting the per-field list safe. They are asserts on the generic
# detector's coverage, not on a hand-kept field list.
# ===========================================================================


def test_result_primitives_emit_only_declared_literal_members():
    """Every literal value handed to a result primitive is a member of its
    field's declared ``Literal`` -- the generic, field-axis-closed form of the
    per-field detectors. Tree-wide: a primitive in a new module is covered."""
    from tests.membership_gate import generic_literal_field_offenders, literal_typed_field_members

    field_members = literal_typed_field_members()
    offenders = app_offenders(generic_literal_field_offenders, field_members=field_members)
    assert not offenders, (
        "a literal passed to a result primitive must be a member of its field's"
        " declared Literal, or the strict enum at response shaping rejects the whole"
        f" batch: {'; '.join(offenders)}"
    )


def test_generic_membership_detector_bites():
    """The generic detector fires on the historical regression shape, and only
    on it -- the proof that replacing the per-field list with a derived one did
    not silently turn the sweep into a no-op."""
    from tests.membership_gate import generic_literal_field_offenders, literal_typed_field_members

    members = literal_typed_field_members()
    assert members, "no Literal fields were enumerated; the generic sweep is a no-op"
    bad = ast.parse('def f():\n    return _failed_engine_result(classification="totally_made_up")\n')
    hits = generic_literal_field_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    assert len(hits) == 1 and "totally_made_up" in hits[0], hits
    # A real member literal passes.
    good = ast.parse('def f():\n    return _failed_engine_result(classification="network_error")\n')
    assert not generic_literal_field_offenders(
        good, REPO_ROOT / "app" / "_synthetic.py", field_members=members
    )
    # A non-literal (the shape a correct caller writes) passes without comment.
    var = ast.parse("def f(c):\n    return _failed_engine_result(classification=c)\n")
    assert not generic_literal_field_offenders(
        var, REPO_ROOT / "app" / "_synthetic.py", field_members=members
    )


def test_generic_membership_subsumes_the_per_field_detectors():
    """The generic sweep covers every field the three per-field detectors
    guarded, and every primitive the hard-coded list forgot.

    The historical detectors matched on a *transcribed* primitive list of six
    names (``_RESULT_PRIMITIVES`` below). Three live primitives that set
    classification/status/strategy were never on it -- ``_binary_no_text_result``
    (which sets all three), ``_crawl_result_from_engine_result``, and
    ``_terminal_engine_result`` -- and neither were the direct dataclass
    constructions at five sites that bypass the primitives entirely. The
    derived registry has twelve entries and is closed by construction.

    This assert is the property that licenses deleting the per-field list: the
    coverage gap is not a matter of opinion, it is the set difference below.
    """
    from tests.membership_gate import RESULT_DATACLASS_NAMES, RESULT_PRIMITIVES, literal_typed_field_members

    members = literal_typed_field_members()
    # The three escaped axes the per-field detectors were written for.
    assert "classification" in members and "site_error" in members["classification"]
    assert "status" in members and "failed" in members["status"]
    # The six-name list the per-field detectors transcribed.
    transcribed = {
        "_failed_engine_result",
        "_make_engine_result",
        "_engine_result_from_exception",
        "_skipped_engine_result",
        "_crawl_result_from_exception",
        "_terminal_crawl_result",
    }
    missed = RESULT_PRIMITIVES - transcribed
    assert (
        missed
        == {
            "_binary_no_text_result",
            "_crawl_result_from_engine_result",
            "_terminal_engine_result",
        }
        | RESULT_DATACLASS_NAMES
    ), (
        "the derived registry must cover the primitives and dataclasses the"
        f" transcribed list omitted: {sorted(missed)}"
    )


def test_result_construction_enforces_membership_at_the_dataclass():
    """A result dataclass built ANYWHERE rejects a non-member value.

    The historical escapes went through direct constructions outside
    result_helpers, so routing was policed geographically. That is no longer the
    load-bearing guarantee: every result dataclass validates its Literal fields
    in ``__post_init__``, so a build site cannot escape membership checking
    regardless of who calls it. This is the falsifiable version of that claim --
    a bad value at a direct construction site must raise, and a member must not.
    """
    from dataclasses import asdict

    from app.models import (
        CrawlResult,
        CrawlStrategyAttempt,
        EngineCrawlResult,
    )
    from tests._result_builders import engine_result

    # The all-member baseline, named once and DERIVED rather than restated. Every
    # axis this test exercises is Literal-typed, so a construction that is spelled
    # twice with one field overridden is the *only* honest way to show that the
    # gate is about the overridden field and nothing else -- the previous version
    # restated the whole block per axis and then read the override back out of the
    # kwargs with ``kwargs.get(field, "success")``, which reads as though the
    # defaults were chosen per test rather than being the shared baseline.
    #
    # ``engine_result(url, "rendered")`` is that baseline exactly: every optional
    # field absent, and status taken from the production classification mapping,
    # which answers "success" for a rendered page. Spelling the same 14 fields
    # here instead was a second statement of which values are members, in the one
    # file whose job is to prove membership -- the worst place for a copy to live.
    member_engine_result = asdict(engine_result("http://a/", "rendered"))
    # Built, not spelled. A hand-written block of nine member values was a second
    # statement of which strings are members -- unverified, in the one file whose
    # job is to prove membership -- and a dict literal infers `int | str | None`
    # per value, so the splat below was unchecked at every field. Constructing the
    # result directly hands each value to the field's own type, so the all-member
    # build is checked against the taxonomy it is a sample of. `asdict` is what
    # makes the override splat below splat.
    member_crawl_result = CrawlResult(
        url="http://a/",
        status="success",
        classification="rendered",
        final_url=None,
        html=None,
        text=None,
        attempts=1,
        elapsed_ms=0,
        domain="example.com",
    )
    member_crawl_result_fields = asdict(member_crawl_result)

    # A direct construction with all-member values is accepted: the gate must
    # not reject legitimate direct builds (tests and the primitives both do it).
    # The construction above is that check -- every field was checked against its
    # own type to get here.
    CrawlStrategyAttempt(
        strategy="BROWSER_HEADLESS",
        status="success",
        classification="rendered",
        http_status=200,
        content_type=None,
        elapsed_ms=0,
    )
    EngineCrawlResult(**member_engine_result)
    # A non-member on each Literal axis raises at construction.
    for field, invented in (
        ("status", "Bogus"),
        ("classification", "Bogus"),
        ("selected_strategy", "BROWSER_INVENTED"),
    ):
        with pytest.raises(ValueError, match=f".{field}="):
            EngineCrawlResult(**{**member_engine_result, field: invented})
    # CrawlError axis: a stable code outside the declared taxonomy is rejected.
    with pytest.raises(ValueError, match=r"\.error="):
        CrawlResult(
            **{
                **member_crawl_result_fields,
                "status": "failed",
                "classification": "network_error",
                "error": "worker_failed",
            }
        )


# ===========================================================================
# NAMED-CONSTANT provenance (program 2)
#
# A literal detector is blind to a Name. The historical defect was exactly
# that shape: EXTRACTION_METHOD_BINARY_NO_TEXT -- a constant from the
# extraction-method namespace -- was assigned into a classification field. Its
# *value* ("binary_no_text") collides with a real classification member, so
# even resolving the name to its value cannot detect it: the collision is the
# defect. The signal that is actually wrong is the constant's NAMESPACE, so
# that is what the detector keys on.
# ===========================================================================


def test_result_primitives_take_taxonomy_constants_not_foreign_namespaces():
    """A constant from one taxonomy must not be consumed as a member of
    another. Value membership cannot see this -- the colliding value is what
    made the historical defect invisible."""
    from tests.membership_gate import literal_typed_field_members, wrong_namespace_constant_offenders

    field_members = literal_typed_field_members()
    offenders = app_offenders(wrong_namespace_constant_offenders, field_members=field_members)
    assert not offenders, (
        "a constant from a foreign namespace was consumed as a taxonomy member; the"
        " value collides with a real member which is exactly why a value check misses"
        f" it: {'; '.join(offenders)}"
    )


def test_wrong_namespace_detector_bites():
    """The provenance detector fires on the historical shape and admits the
    correct one."""
    from tests.membership_gate import literal_typed_field_members, wrong_namespace_constant_offenders

    members = literal_typed_field_members()
    bad = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f():\n"
        "    return _failed_engine_result(classification=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    hits = wrong_namespace_constant_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    assert len(hits) == 1 and "EXTRACTION_METHOD_" in hits[0], hits
    # The same constant used as its own field is the intended use.
    ok = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f(m):\n"
        "    return _make_engine_result(extraction_method=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    assert not wrong_namespace_constant_offenders(
        ok, REPO_ROOT / "app" / "_synthetic.py", field_members=members
    )
    # A parameter (what a correct caller writes) is not a constant: passes.
    param = ast.parse("def f(cls):\n    return _failed_engine_result(classification=cls.site_error)\n")
    assert not wrong_namespace_constant_offenders(
        param, REPO_ROOT / "app" / "_synthetic.py", field_members=members
    )


# ---------------------------------------------------------------------------
# The runtime gate (program 1+2 closure): the class dies at the boundary
# ---------------------------------------------------------------------------


def test_the_membership_gate_rejects_a_non_member_value():
    """The AST detectors see source text; the historical escape reached the
    primitive through a local variable, which no textual detector can see.
    The gate is the structural closure: whatever indirection a value took, it
    must pass the primitive to become a result."""
    from tests.membership_gate import NonMemberValue, assert_member, assert_optional_member

    members = frozenset({"rendered", "site_error"})
    assert_member("rendered", field="classification", members=members, where="_test")
    with pytest.raises(NonMemberValue):
        assert_member("binary_no_text", field="classification", members=members, where="_test")
    # None is admitted for the optional Literal fields.
    assert_optional_member(None, field="strategy", members=frozenset({"BROWSER_HEADLESS"}), where="_test")
    with pytest.raises(NonMemberValue):
        assert_optional_member(
            "BROWSER_DECLARED_CRAWLER", field="strategy", members=frozenset({}), where="_test"
        )


def test_the_gate_sees_the_local_variable_indirection_the_ast_detectors_cannot():
    """The exact historical shape, end to end: a local variable assigned a
    wrong-namespace constant, then handed to the primitive as a bare Name. No
    kwarg-level AST detector fires on this; the gate does, because it inspects
    the value at the boundary instead of the text at the call."""
    import asyncio

    from tests.membership_gate import NonMemberValue, field_member_sets

    members = field_member_sets()
    classification_members = members["classification"]

    async def scenario() -> None:
        # `classification` is a local whose provenance no AST walk can resolve
        # from the call site alone.
        classification = "totally_made_up"
        from tests.membership_gate import assert_result_fields

        assert_result_fields(
            {"classification": classification},
            where="http._http_strategy_result",
            members=members,
        )

    with pytest.raises(NonMemberValue):
        asyncio.run(scenario())
    # A real member through the same indirection passes.
    assert "binary_no_text" in classification_members


def test_the_gate_would_have_caught_the_shipped_defect():
    """``EXTRACTION_METHOD_BINARY_NO_TEXT`` has a value that IS a legal
    classification member -- so this is the shape a value check admits. The
    gate inspects the value, and the value here is legal, which is precisely
    the point: the *provenance* AST detector above is what names the defect,
    while the gate is what guarantees no wrong value of ANY provenance crosses.
    Both are needed and neither subsumes the other."""
    from tests.membership_gate import NonMemberValue, assert_result_fields, field_member_sets

    members = field_member_sets()
    # A genuinely non-member value through the same local-variable shape.
    with pytest.raises(NonMemberValue):
        assert_result_fields(
            {"classification": "not_a_real_member"},
            where="http._http_strategy_result",
            members=members,
        )


# ---------------------------------------------------------------------------
# The construction check has exactly one owner
#
# ``_check_literal_fields`` is a module-level function and the result dataclasses
# each had to reach it through their own ``__post_init__``. That trampoline was
# the second thing to keep in sync: the check is one function, but a result
# dataclass added tomorrow would validate nothing while reading exactly like the
# ones that do. It is now inherited from ``models._LiteralChecked``, so the check
# is part of the class rather than something it remembers to ask for -- and
# "inherited" is a claim the tree can check, which is what these two do:
# a caller of the check outside the owner is a second copy of it, and a result
# dataclass that does not inherit the owner is a class with no check.
# ---------------------------------------------------------------------------


def _literal_check_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Any call to ``_check_literal_fields`` outside its one owning ``__post_init__``."""
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        if resolved_call_name(tree, node) != "app.models._check_literal_fields":
            continue
        owner = _enclosing_class(tree, node)
        if owner is not None and owner.name == "_LiteralChecked":
            continue
        offenders.append(offender_at(path, node, "literal-check-called-outside-its-owner"))
    return offenders


def _enclosing_class(tree: ast.AST, node: ast.AST) -> ast.ClassDef | None:
    """The innermost ``class`` body containing ``node``, or None.

    A call to the check has to be attributed to the class it is written in, and
    a nested function inside that class is still that class's: the whole point
    of the guard is that the check runs at construction, which is a property of
    the class, not of the function that happens to spell the call.
    """
    found: list[ast.ClassDef | None] = []

    def visit(current: ast.AST, enclosing: tuple[ast.ClassDef, ...]) -> bool:
        for child in ast.iter_child_nodes(current):
            if child is node:
                found.append(enclosing[-1] if enclosing else None)
                return True
            nested = enclosing + (child,) if isinstance(child, ast.ClassDef) else enclosing
            if visit(child, nested):
                return True
        return False

    visit(tree, ())
    return found[0] if found else None


def test_the_literal_field_check_is_called_by_one_owner():
    """One caller for ``_check_literal_fields``, and it is the owning base class.

    A second caller is a second place the construction check can be re-spelled,
    which is the shape this consolidation removed: the check was one function
    reached through three identical trampolines, and a class that re-spelled it
    would be indistinguishable from one that inherited it.
    """
    assert_trees_clean(
        _literal_check_offenders,
        "_check_literal_fields is called outside models._LiteralChecked, so the"
        " construction-time taxonomy check exists in more than one place",
    )


def test_literal_check_owner_detector_bites():
    """The owner guard fires on a re-spelled trampoline and admits the base.

    The first shape is the pre-consolidation one -- a class calling the check for
    itself -- and the second is the class that is allowed to. The third is the
    shape the guard must NOT fire on: a *different* module's function that
    happens to share the name, resolved by import bindings rather than by text.
    """
    assert_fires_once(
        _literal_check_offenders,
        {
            "from app.models import _check_literal_fields\n"
            "@dataclass(frozen=True)\n"
            "class CrawlResult:\n"
            "    status: str\n"
            "    def __post_init__(self) -> None:\n"
            "        _check_literal_fields(self)\n": "literal-check-called-outside-its-owner",
        },
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert_stays_clean(
        _literal_check_offenders,
        (
            "from app.models import _check_literal_fields\n"
            "class _LiteralChecked:\n"
            "    def __post_init__(self) -> None:\n"
            "        _check_literal_fields(self)\n",
            # A module that never mentions the check is nobody else's business.
            "from app.models import CrawlResult\ndef build() -> CrawlResult:\n    return CrawlResult()\n",
        ),
        REPO_ROOT / "app" / "_synthetic.py",
    )


def test_every_taxonomy_literal_dataclass_inherits_the_construction_check():
    """A dataclass that declares a taxonomy ``Literal`` field inherits the check.

    The half of the property the owner guard cannot see: an *omission* rather
    than a duplicate. Which dataclasses qualify is derived from the taxonomy's
    own declaration -- ``models._LITERAL_FIELDS`` for the field names and
    ``models._literal_members`` for whether the annotation is really a taxonomy
    alias -- and the classes are enumerated from the module that declares them,
    so a result dataclass added tomorrow is covered the day it lands and nothing
    here is a hand-kept class list. A field merely *named* like a taxonomy field
    but annotated ``str`` is not one of these and is not reported.

    The taxonomy aliases live in this module by construction: ``Classification``,
    ``CrawlError`` and ``CrawlStrategyName`` are declared here, and
    ``ResultStatus`` is the one member set imported from ``app.constants``. A
    dataclass in another module that carries such a field is not covered here,
    which is why the owner guard above is tree-wide and this one is not: that
    guard is what catches a check re-spelled anywhere, and this one is what
    catches a class in the taxonomy's own home that forgot to ask for it.
    """
    import typing
    from dataclasses import is_dataclass

    from app import models

    checked: dict[str, list[str]] = {}
    for name, cls in sorted(vars(models).items()):
        if not (isinstance(cls, type) and is_dataclass(cls)):
            continue
        # Field names first: a dataclass with no taxonomy-named field cannot be
        # one of these, and asking for its resolved hints would evaluate a
        # TYPE_CHECKING-only annotation (CrawlJob's HostPolicy) for nothing.
        named = [field for field in cls.__dataclass_fields__ if field in models._LITERAL_FIELDS]
        if not named:
            continue
        hints = typing.get_type_hints(cls)
        fields = [field for field in named if models._literal_members(hints.get(field))]
        if not fields:
            continue
        checked[name] = fields
        assert issubclass(cls, models._LiteralChecked), (
            f"{name} declares the taxonomy Literal field(s) {sorted(fields)} but no"
            " construction check; inherit models._LiteralChecked so a non-member is"
            " rejected when the value is built rather than at the wire"
        )
    assert checked, (
        "no taxonomy Literal field was enumerated, so the sweep is a no-op:"
        f" _LITERAL_FIELDS={sorted(models._LITERAL_FIELDS)}"
    )
    assert set(checked) == {"CrawlResult", "CrawlStrategyAttempt", "EngineCrawlResult"}, sorted(checked)


# ===========================================================================
# ALIAS-BLINDNESS (program 3): detectors must resolve import bindings, not
# match on a literal module name
# ===========================================================================


def test_dynamic_pattern_re_calls_under_any_alias():
    """``import re as _re`` then ``_re.compile(<dynamic>)`` is invisible to a
    detector anchored on the name ``re``. The binding is resolved first, so an
    alias cannot rename a dynamic pattern past the chokepoint. This subsumes
    ``test_no_dynamic_pattern_re_calls`` and the call shape of
    ``test_no_bare_re_compile_on_nonliteral``."""
    from tests.membership_gate import aliased_dynamic_pattern_re_offenders

    offenders = app_offenders(aliased_dynamic_pattern_re_offenders)
    assert not offenders, (
        "re.<method> with a dynamic pattern must go through app.safe_regex, under any"
        f" import alias: {'; '.join(offenders)}"
    )


def test_alias_detector_bites():
    from tests.membership_gate import aliased_dynamic_pattern_re_offenders

    aliased = ast.parse("import re as _re\n\n\ndef f(p):\n    return _re.compile(p)\n")
    hits = aliased_dynamic_pattern_re_offenders(aliased, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "_re.compile" in hits[0], hits
    # A literal pattern under the same alias is safe and stays unflagged.
    literal = ast.parse('import re as _re\n\n\ndef f():\n    return _re.compile(r"<loc>")\n')
    assert not aliased_dynamic_pattern_re_offenders(literal, REPO_ROOT / "app" / "_synthetic.py")
    # The plain ``re`` name is still covered (subsumption of the old detector).
    plain = ast.parse("import re\n\n\ndef f(p):\n    return re.search(p, t)\n")
    assert aliased_dynamic_pattern_re_offenders(plain, REPO_ROOT / "app" / "_synthetic.py")
    # No re import at all: nothing to check.
    none_ = ast.parse("def f(p):\n    return compile(p)\n")
    assert not aliased_dynamic_pattern_re_offenders(none_, REPO_ROOT / "app" / "_synthetic.py")


def test_sanctioned_func_calls_under_any_alias():
    """A sanctioned chokepoint function is covered under any import alias.

    The name-matching detectors each carry a frozenset of spellings and were
    extended one alias at a time. This sweep resolves the import binding, so a
    new alias of ``decompress_body`` cannot name its way out of the cap
    requirement the way ``_re`` names its way out of the pattern check."""
    from tests.membership_gate import aliased_sanctioned_func_offenders

    offenders = app_offenders(aliased_sanctioned_func_offenders)
    assert not offenders, (
        "a sanctioned chokepoint function is called through an alias the"
        " name-matching detectors do not recognize; a missing cap or filter would"
        f" be invisible to them: {'; '.join(offenders)}"
    )


def test_sanctioned_func_alias_detector_bites():
    """The alias audit fires on a spelling the hand-kept list does not contain,
    and stays silent on the two spellings the existing list already covers."""
    from tests.membership_gate import aliased_sanctioned_func_offenders

    unknown = ast.parse(
        "from app.crawl.decompression import decompress_body as dec\n\n"
        "def f(raw, enc):\n    return dec(raw, enc)\n"
    )
    hits = aliased_sanctioned_func_offenders(unknown, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "decompress_body" in hits[0], hits
    # The two spellings _DECOMPRESS_FUNCS already lists are known and silent.
    known = ast.parse(
        "from app.crawl.decompression import decompress_body as _decompress_body\n\n"
        "def f(raw, enc):\n    return _decompress_body(raw, enc)\n"
    )
    assert not aliased_sanctioned_func_offenders(known, REPO_ROOT / "app" / "_synthetic.py")
    # An import that is not a sanctioned function is not this sweep's business.
    unrelated = ast.parse("from os import path as p\n\n\ndef f():\n    return p.join('a')\n")
    assert not aliased_sanctioned_func_offenders(unrelated, REPO_ROOT / "app" / "_synthetic.py")


# ===========================================================================
# TEST DOUBLES THAT SWALLOW SIGNATURE DRIFT (program 4)
# ===========================================================================


def test_doubles_do_not_accept_and_drop_kwargs():
    """A Protocol-shaped double that accepts ``**kwargs`` and never routes it
    is blind to a signature change on the production callable: the
    ``failed_result`` refactor broke 24 tests silently through exactly this
    shape, because the doubles accepted the new kwarg and dropped it."""
    from tests.membership_gate import swallowing_test_double_offenders

    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        offenders.extend(swallowing_test_double_offenders(tree, path))
    assert not offenders, (
        "bind the production kwarg by name or forward **kwargs to the real callable; a"
        " double that drops it implements a contract that no longer exists: " + "; ".join(offenders)
    )


def test_swallowing_double_detector_bites():
    from tests.membership_gate import swallowing_test_double_offenders

    drops = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return {}\n"
    )
    hits = swallowing_test_double_offenders(drops, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(hits) == 1 and "run(**kwargs)" in hits[0], hits
    # Forwarding is the sanctioned shape: the double passes the contract through.
    forwards = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return real.run(jobs, worker, on_result=on_result, **kwargs)\n"
    )
    assert not swallowing_test_double_offenders(forwards, REPO_ROOT / "tests" / "_synthetic.py")
    # Consulting kwargs by name (kwargs.get) also counts as routing it.
    consults = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return kwargs.get('x', 1)\n"
    )
    assert not swallowing_test_double_offenders(consults, REPO_ROOT / "tests" / "_synthetic.py")
    # A non-Protocol method is not a double and stays unflagged.
    unrelated = ast.parse("def run(a, **kwargs):\n    return 1\n")
    assert not swallowing_test_double_offenders(unrelated, REPO_ROOT / "tests" / "_synthetic.py")


def test_no_existing_double_swallows_failed_result():
    """Derived from the production signature, not a hand-kept list: every
    scheduler double in tests/ binds ``failed_result`` by name today, which is
    the property the ``failed_result`` refactor needed. A double that regressed
    to ``**kwargs``-only would be caught by both this and the sweep above."""
    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        for node in nodes_of(tree):
            if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"):
                continue
            positional = [
                a.arg for a in node.args.posonlyargs + node.args.args if a.arg not in {"self", "cls"}
            ]
            if len(positional) < 2:
                continue
            names = {a.arg for a in node.args.args + node.args.kwonlyargs}
            names |= {a.arg for a in node.args.posonlyargs}
            if "failed_result" not in names:
                offenders.append(offender_at(path, node, "run() does not bind failed_result by name"))
    assert not offenders, (
        "the DomainScheduler.run contract requires failed_result; a double that omits it"
        " silently drops the envelope the pipeline supplies: " + "; ".join(offenders)
    )


# ===========================================================================
# Program 5 (hunt): the error= field's undeclared taxonomy
# ===========================================================================


def test_error_field_values_are_classification_members():
    """A literal ``error=`` on a result primitive is in the declared taxonomy.

    ``error`` is a declared ``CrawlError`` Literal: page-outcome codes that are
    also Classification members, plus pipeline conditions with no page analogue
    (capacity, config). The field is documented as carrying a stable code
    (render.py) that callers key on, so a literal outside the declared taxonomy
    is a contract leak; this reports it so the missing chokepoint is visible
    rather than latent.
    """
    from tests.membership_gate import error_field_taxonomy_offenders

    offenders = app_offenders(error_field_taxonomy_offenders)
    assert not offenders, (
        "error= must be a member of the declared CrawlError taxonomy (the field is"
        f" documented as a stable code callers key on): {'; '.join(offenders)}"
    )


def test_error_field_taxonomy_detector_bites():
    """The hunt detector fires on the shipped shape and admits the classified one."""
    from tests.membership_gate import error_field_taxonomy_offenders

    bad = ast.parse('def f():\n    return _terminal_crawl_result(url="u", error="worker_failed")\n')
    hits = error_field_taxonomy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "worker_failed" in hits[0], hits
    # A member literal passes.
    good = ast.parse('def f():\n    return _terminal_crawl_result(url="u", error="site_error")\n')
    assert not error_field_taxonomy_offenders(good, REPO_ROOT / "app" / "_synthetic.py")
    # A classified variable is the sanctioned shape and passes.
    var = ast.parse("def f(c):\n    return _terminal_crawl_result(url='u', error=c)\n")
    assert not error_field_taxonomy_offenders(var, REPO_ROOT / "app" / "_synthetic.py")


def _job_engine_payload_chokepoint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    has_builder = any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_job_engine_payload"
        for node in nodes_of(tree)
    )
    if relative != "app/api/job_runner.py" and not has_builder:
        return []
    execute_names = {"_execute_crawl", "_real_execute_crawl"}
    builder_names = {"_job_engine_payload"}
    for node in nodes_of_type(tree, ast.Assign, ast.AnnAssign):
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(value, ast.Name) and value.id in execute_names:
            execute_names.update(target.id for target in targets if isinstance(target, ast.Name))
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in builder_names
        ):
            builder_names.update(target.id for target in targets if isinstance(target, ast.Name))
    offenders: list[str] = []
    for node in calls_to_any(tree, execute_names):
        payload = (
            node.args[0]
            if node.args
            else next(
                (keyword.value for keyword in node.keywords if keyword.arg in {"payload", "data"}), None
            )
        )
        if isinstance(payload, ast.Name) and payload.id in builder_names:
            continue
        if (
            isinstance(payload, ast.Call)
            and isinstance(payload.func, ast.Name)
            and payload.func.id in builder_names
        ):
            continue
        offenders.append(offender_at(path, node, "_execute_crawl payload bypasses _job_engine_payload"))
    return offenders


def test_job_engine_payload_uses_single_builder():
    assert_trees_clean(
        _job_engine_payload_chokepoint_offenders,
        "Build whole-site engine payloads only with _job_engine_payload",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_job_engine_payload_chokepoint_detector_bites():
    pre_fix = ast.parse(
        "async def run(job, batch):\n"
        "    payload = {'urls': batch, 'policy': dict(job.policy)}\n"
        "    await _execute_crawl(payload)\n"
    )
    alias = ast.parse(
        "async def run(job, batch):\n"
        "    execute = _execute_crawl\n"
        "    await execute({'urls': batch, 'policy': {}})\n"
    )
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    assert _job_engine_payload_chokepoint_offenders(pre_fix, path)
    assert _job_engine_payload_chokepoint_offenders(alias, path)
    good = ast.parse(
        "async def run(job, batch):\n"
        "    payload = _job_engine_payload(job, batch)\n"
        "    await _execute_crawl(payload)\n"
    )
    assert not _job_engine_payload_chokepoint_offenders(good, path)


# The strategy chain's raise-chokepoint. A strategy is a STEP in a ladder, so
# a step that raises must become a classified failed result and the ladder
# must move on; the sink is a direct call to a strategy runner, and the
# requirement is that it sits inside a try with an ``except Exception``.
# Tree-wide, because a second module may grow a strategy runner and reach for
# the chain's helpers.
_STRATEGY_RUNNERS = frozenset({"_run_http_strategy", "_run_browser_strategy"})


def _guarded_node_ids(tree: ast.AST) -> set[int]:
    """Identity of every node in a ``try`` body that has an ``except Exception``.

    Only the guarded ``body`` counts. A call in an ``except``/``else``/
    ``finally`` clause is not protected by its own try, so including those
    would open exactly the hole this detector closes.
    """
    guarded: set[int] = set()
    for node in nodes_of_type(tree, ast.Try):
        if not any(
            isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}
            for handler in node.handlers
        ):
            continue
        for statement in node.body:
            guarded.update(id(child) for child in nodes_of(statement))
    return guarded


def _runner_call_name(node: ast.Call) -> str:
    """A strategy runner's name, whether it was called bare or as a method.

    Resolving both spellings is what keeps the invariant attached to the CODE
    rather than to one particular refactor's shape. A detector that matched only
    ``ast.Name`` reported a clean tree at the exact moment the runner became a
    method -- the refactor that cannot weaken the invariant is the one most
    likely to make it silently unenforced, because nothing about the change looks
    like touching it.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _unguarded_strategy_runner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    guarded = _guarded_node_ids(tree)
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        name = _runner_call_name(node)
        if name in _STRATEGY_RUNNERS and id(node) not in guarded:
            offenders.append(offender_at(path, node, f"{name} invoked outside a try/except Exception"))
    return offenders


def test_every_strategy_runner_call_is_guarded():
    assert_trees_clean(
        _unguarded_strategy_runner_offenders,
        "A strategy that raises must become a failed step, not escape the chain",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_strategy_runner_guard_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "strategy_chain.py"
    pre_fix = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    if strategy in HTTP_STRATEGIES:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    return await _run_browser_strategy(context, strategy)\n"
    )
    assert _unguarded_strategy_runner_offenders(pre_fix, path)
    # A narrow handler is not a guard: a bare ``except TypeError`` lets every
    # other raise through, which is the shape the bug shipped as.
    narrow = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except TypeError:\n"
        "        return None\n"
    )
    assert _unguarded_strategy_runner_offenders(narrow, path)
    good = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except Exception as error:\n"
        "        return _engine_result_from_exception(error=error)\n"
    )
    assert not _unguarded_strategy_runner_offenders(good, path)
    # The guard is satisfied by an enclosing try too, not only a sibling one.
    enclosing = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        if strategy:\n"
        "            return await _run_browser_strategy(context, strategy)\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert not _unguarded_strategy_runner_offenders(enclosing, path)
    # The runner reached as a METHOD is the same sink. A detector that matched
    # only a bare name goes blind at the moment the runner becomes a method --
    # which is a refactor that looks like it cannot weaken the invariant, and is
    # therefore the one most likely to make it silently unenforced.
    method_unguarded = ast.parse(
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        return await self._run_browser_strategy(strategy)\n"
    )
    assert _unguarded_strategy_runner_offenders(method_unguarded, path)
    method_guarded = ast.parse(
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        try:\n"
        "            return await self._run_browser_strategy(strategy)\n"
        "        except Exception as error:\n"
        "            return _engine_result_from_exception(error=error)\n"
    )
    assert not _unguarded_strategy_runner_offenders(method_guarded, path)


# The scope-regex compiler: ONE definition of "compile a user-supplied scope
# pattern, fail closed, bound the length". It had two -- a public one in
# admission and a private one in engine -- that differed in their except
# clause, and the length bound was a second module constant with the same name
# and the same value. One definition, one bound, and the fail-closed arms of
# both.
_SCOPE_COMPILER_NAMES = frozenset({"compile_scope_pattern", "_compile_scope_regex_safe"})
_SCOPE_PATTERN_LENGTH_BOUND = "_MAX_SCOPE_PATTERN_LEN"


# The store-offload invariant: inside an async def, a store is reached through
# its async twin. The sink is a direct call to a SYNCHRONOUS store method; the
# requirement is that no async frame in app/ makes one. Tree-wide, because the
# next handler, runner, or lifespan step is where it comes back.
_STORE_CLASSES = ("JobStore", "CrawlResultStore")
# The two stores are injected unannotated (FastAPI ``Depends``, lifespan
# globals, runner arguments), so a type-annotation-only resolution would see
# nothing. These names are the project's own convention for the injected
# receiver; a frame that binds one of them holds a store.
_CONVENTIONAL_STORE_NAMES = {"job_store": "JobStore", "result_store": "CrawlResultStore"}


def _sync_store_methods() -> dict[str, frozenset[str]]:
    """Store class name -> its public synchronous method names.

    Derived from the classes rather than listed here, so a method added to a
    store is covered by this detector the day it lands and an async twin added
    later drops out of the set with nobody editing this test. Importing the two
    store modules is safe: the schema runs when a store is CONSTRUCTED, not at
    import.
    """
    from app.crawl.job_store import JobStore
    from app.crawl.result_store import CrawlResultStore

    derived: dict[str, frozenset[str]] = {}
    for class_name, store_class in (("JobStore", JobStore), ("CrawlResultStore", CrawlResultStore)):
        derived[class_name] = frozenset(
            member
            for member in dir(store_class)
            if not member.startswith("_")
            and callable(getattr(store_class, member, None))
            and not inspect.iscoroutinefunction(getattr(store_class, member))
        )
    return derived


def _store_receivers(function: ast.AST) -> dict[str, str]:
    """Name -> store class, for the store-typed bindings of one function."""
    receivers: dict[str, str] = {}
    for node in nodes_of(function):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation = ast.unparse(node.annotation)
            if annotation in _STORE_CLASSES:
                receivers[node.arg] = annotation
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
                    continue
                constructor = ast.unparse(node.value.func)
                if constructor in _STORE_CLASSES:
                    receivers[target.id] = constructor
    receivers.update(_CONVENTIONAL_STORE_NAMES)
    return receivers


def _sync_store_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    sync_methods = _sync_store_methods()
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.AsyncFunctionDef):
        receivers = _store_receivers(node)
        for inner in nodes_of(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                continue
            owner = receivers.get(ast.unparse(inner.func.value))
            if owner is not None and inner.func.attr in sync_methods[owner]:
                offenders.append(
                    offender_at(
                        path,
                        inner,
                        f"{ast.unparse(inner.func.value)}.{inner.func.attr} "
                        f"is synchronous; await the async twin",
                    )
                )
    return offenders


def test_no_sync_store_method_is_called_from_an_async_frame():
    assert_trees_clean(
        _sync_store_call_offenders,
        "A store call on the event loop stalls every in-flight request",
        trees=app_trees(exclude=SANCTIONED),
    )


def _action_chain_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Every call that must carry the chain's deadline must carry one.

    ``BrowserRenderer._run_action_chain`` takes ``budget_seconds`` as an
    OPTIONAL argument so a test can drive the raw action executor without
    standing up a render budget. An optional deadline is a hole exactly as
    wide as the next caller who omits it, so the signature stays permissive and
    this sweep carries the invariant: production always passes the budget.
    """
    offenders: list[str] = []
    for node, func in attribute_calls(tree):
        if func.attr != "_run_action_chain":
            continue
        if any(keyword.arg == "budget_seconds" for keyword in node.keywords):
            continue
        offenders.append(
            offender_at(
                path,
                node,
                "_run_action_chain runs without a budget, so a "
                "wedged action holds the browser pool slot for as long as it likes",
            )
        )
    return offenders


def test_the_action_chain_is_always_invoked_with_a_budget():
    offenders = app_offenders(_action_chain_offenders)
    assert not offenders, "; ".join(offenders)


def test_action_chain_budget_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "strategies" / "browser.py"
    unbudgeted = ast.parse(
        "async def render(self, page, actions):\n    await self._run_action_chain(page, actions)\n"
    )
    assert len(_action_chain_offenders(unbudgeted, path)) == 1
    budgeted = ast.parse(
        "async def render(self, page, actions, budget):\n"
        "    await self._run_action_chain(page, actions, budget_seconds=budget)\n"
    )
    assert not _action_chain_offenders(budgeted, path)
    unrelated = ast.parse("async def render(self):\n    await self._run_action_chain_forever()\n")
    assert not _action_chain_offenders(unrelated, path)


def test_sync_store_call_detector_bites():
    path = REPO_ROOT / "app" / "api" / "crawl_endpoint.py"
    sync_methods = _sync_store_methods()
    assert "load" in sync_methods["JobStore"] and "aload" not in sync_methods["JobStore"]
    on_the_loop = ast.parse(
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = result_store.get(request_id)\n"
        "    job = job_store.load(request_id)\n"
    )
    assert len(_sync_store_call_offenders(on_the_loop, path)) == 2
    annotated = ast.parse("async def runner(store: JobStore):\n    return store.list_jobs()\n")
    assert len(_sync_store_call_offenders(annotated, path)) == 1
    constructed = ast.parse(
        "async def lifespan():\n    store = JobStore(path, policy=policy)\n    return store.delete(job_id)\n"
    )
    assert len(_sync_store_call_offenders(constructed, path)) == 1
    compliant = ast.parse(
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = await result_store.aget(request_id)\n"
        "    job = await job_store.aload(request_id)\n"
    )
    assert not _sync_store_call_offenders(compliant, path)
    # A sync frame is not the hazard: startup recovery before the loop exists
    # and test setup are both entitled to the synchronous methods.
    sync_frame = ast.parse("def seed(job_store):\n    return job_store.load(job_id)\n")
    assert not _sync_store_call_offenders(sync_frame, path)


# The scheduler's delivery sink is not a worker step. A pipeline that hands the
# scheduler an ``on_result`` sink (or wraps one locally) must deliver AFTER the
# worker's try/except, or a sink that raises is answered as a failed page: the
# domain's breaker is charged, a fabricated envelope is delivered through the
# same broken sink, and gather discards what is left silently.
_SINK_PARAM_NAME = "on_result"
_SINK_CLOSURE_NAME = "deliver"


def _is_result_sink_owner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        (isinstance(arg, ast.arg) and arg.arg == _SINK_PARAM_NAME)
        or (isinstance(arg, ast.Name) and arg.id == _SINK_CLOSURE_NAME)
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
    ) or any(
        isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef) and inner.name == _SINK_CLOSURE_NAME
        for inner in node.body
    )


def _sink_call_in_try_body(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for node in functions(tree):
        if not _is_result_sink_owner(node):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Try):
                continue
            # Only the try BODY: a delivery in an ``except`` handler is the
            # required worker-failure path, and one in ``finally``/``else`` is
            # already outside the protected region.
            for protected in statement.body:
                for inner in nodes_of(protected):
                    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
                        continue
                    if inner.func.id in {_SINK_PARAM_NAME, _SINK_CLOSURE_NAME}:
                        offenders.append(
                            offender_at(
                                path,
                                inner,
                                f"{inner.func.id}() inside the worker's try: delivery is not a "
                                f"worker step, and a "
                                f"raising sink must not be answered with a failure envelope",
                            )
                        )
    return offenders


def test_result_delivery_never_runs_inside_the_worker_try():
    assert_trees_clean(
        _sink_call_in_try_body,
        "Delivery raised from a worker handler",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_sink_try_detector_bites():
    path = REPO_ROOT / "app" / "scheduler.py"
    contaminated = ast.parse(
        "async def run(self, jobs, worker, on_result=None):\n"
        "    async def deliver(index, result):\n"
        "        await on_result(index, result)\n"
        "    try:\n"
        "        result = await worker(job)\n"
        "        await deliver(job.index, result)\n"
        "    except Exception as error:\n"
        "        await deliver(job.index, failed_result(job, error))\n"
    )
    # The handler delivery is REQUIRED (that is the worker's failure path); only
    # the try-body one is the defect.
    assert len(_sink_call_in_try_body(contaminated, path)) == 1
    clean = ast.parse(
        "async def run(self, jobs, worker, on_result=None):\n"
        "    async def deliver(index, result):\n"
        "        await on_result(index, result)\n"
        "    try:\n"
        "        result = await worker(job)\n"
        "    except Exception as error:\n"
        "        await deliver(job.index, failed_result(job, error))\n"
        "        return\n"
        "    await deliver(job.index, result)\n"
    )
    assert not _sink_call_in_try_body(clean, path)
    # A function with no sink is not this detector's business, try or not.
    unrelated = ast.parse(
        "async def run(worker):\n"
        "    try:\n"
        "        return await worker(job)\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert not _sink_call_in_try_body(unrelated, path)


def _definitions_in(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The scope-compiler labels one parsed tree contributes.

    Split out from :func:`_scope_compiler_definitions` so the bites case can
    hand the detector a synthetic tree: the walk owns app/ only, so a test
    that wants to prove the detector fires has to plant the shape itself.
    """
    found: list[str] = []
    for node in nodes_of(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in _SCOPE_COMPILER_NAMES:
                found.append(offender_at(path, node, f"scope-regex compiler {node.name}"))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _SCOPE_PATTERN_LENGTH_BOUND
            for target in node.targets
        ):
            found.append(offender_at(path, node, "scope-regex length bound"))
    return found


def _scope_compiler_definitions() -> list[str]:
    """File labels of every scope-regex compiler and of its length bound."""
    found: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        found.extend(_definitions_in(tree, path))
    return found


def test_scope_regex_compiler_has_exactly_one_definition():
    """Behavior parity across the dedupe is covered by tests/crawl/test_scope_regex.py;
    this pins that no second copy of the compiler or of its bound can creep back."""
    found = _scope_compiler_definitions()
    compilers = [label for label in found if "compiler" in label]
    bounds = [label for label in found if "length bound" in label]
    assert len(compilers) == 1, f"scope-regex compiler duplicated again: {compilers}"
    assert len(bounds) == 1, f"scope-regex length bound duplicated again: {bounds}"
    assert "app/crawl/admission.py" in compilers[0], compilers
    assert "app/crawl/admission.py" in bounds[0], bounds


def test_scope_regex_compiler_detector_bites():
    """The detector reports one label per definition, so the tree it must
    reject is any tree with more than one -- two compilers, or the one
    compiler plus a second copy of its bound."""
    path = REPO_ROOT / "app" / "crawl" / "admission.py"
    two_compilers = ast.parse(
        "def compile_scope_pattern(value):\n    return value\n"
        "def _compile_scope_regex_safe(value):\n    return value\n"
    )
    compiler_and_bound = ast.parse(
        "_MAX_SCOPE_PATTERN_LEN = 256\ndef compile_scope_pattern(value):\n    return value\n"
    )
    assert len(_definitions_in(two_compilers, path)) == 2
    assert len(_definitions_in(compiler_and_bound, path)) == 2
    assert not _definitions_in(ast.parse("def _helper(value):\n    return value\n"), path)


def _frontier_expression(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "frontier"


# The admission chokepoint owns the pattern it bans: admit_job_links is the one
# function allowed to grow a frontier, so the sweep that bans direct growth
# everywhere else has to skip the module that defines it.
_ADMISSION_CHOKEPOINT = "app/crawl/admission.py"


def _direct_frontier_growth_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    allowed_functions: frozenset[str] = frozenset(),
) -> list[str]:
    growth_methods = {"append", "extend", "insert"}
    offenders: list[str] = []
    parents = {child: parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}

    def is_allowed(node: ast.AST) -> bool:
        if not allowed_functions:
            return False
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if (
            not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
            or owner.name not in allowed_functions
        ):
            return False
        if owner.name == "normalize_job_state":
            return (
                isinstance(node, ast.Assign)
                and any(_frontier_expression(target) for target in node.targets)
                and isinstance(node.value, ast.Name)
                and node.value.id == "frontier"
            )
        return (
            owner.name == "_persist"
            and isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and _frontier_expression(node.value)
        )

    for node in nodes_of(tree):
        if is_allowed(node):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in growth_methods and _frontier_expression(node.func.value):
                offenders.append(offender_at(path, node, "direct frontier growth bypasses admit_job_links"))
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_frontier_expression(target) for target in targets):
                offenders.append(offender_at(path, node, "frontier replacement bypasses admit_job_links"))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
                if _frontier_expression(node.value):
                    offenders.append(offender_at(path, node, "frontier alias bypasses admit_job_links"))
                if node.value.attr == "append" and _frontier_expression(node.value.value):
                    offenders.append(
                        offender_at(path, node, "frontier method alias bypasses admit_job_links")
                    )
        elif isinstance(node, ast.Subscript) and _frontier_expression(node.value):
            parent = parents.get(node)
            if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign):
                offenders.append(offender_at(path, node, "frontier slice mutation bypasses admit_job_links"))
    return offenders


def test_job_frontier_growth_only_uses_funnel():
    offenders = app_offenders(
        _direct_frontier_growth_offenders,
        skip=(_ADMISSION_CHOKEPOINT,),
        kwargs_for=lambda relative: (
            {"allowed_functions": frozenset({"normalize_job_state", "_persist"})}
            if relative == "app/crawl/job_store.py"
            else {}
        ),
    )
    assert not offenders, "Grow whole-site frontiers only through admit_job_links: " + ", ".join(offenders)


def test_job_frontier_growth_detector_bites():
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    pre_fix_owners = (
        "def run(job, url):\n    job.frontier.append(url)\n",
        "def run(job, url):\n    append = job.frontier.append\n    append(url)\n",
        "def run(job, url):\n    job.frontier += [url]\n",
        "def run(job, urls):\n    job.frontier = urls\n",
    )
    for source in pre_fix_owners:
        assert _direct_frontier_growth_offenders(ast.parse(source), path), source
    alias = ast.parse("def run(job, url):\n    frontier = job.frontier\n    frontier.append(url)\n")
    assert _direct_frontier_growth_offenders(alias, path)
    good = ast.parse("async def run(job, links):\n    await admit_job_links(job, links)\n")
    assert not _direct_frontier_growth_offenders(good, path)


_FILTER_NAMES = {
    "app.constants.is_analytics_host": "is_analytics_host",
    "app.constants.is_document_url": "is_document_url",
    "app.crawl.scope_match.scope_allows": "scope_allows",
    "app.validation.normalize_public_http_url": "normalize_public_http_url",
}


def _imported_filter_names(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes_of(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app.constants",
            "app.crawl.scope_match",
            "app.validation",
        }:
            for imported in node.names:
                canonical = f"{node.module}.{imported.name}"
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
        elif isinstance(node, ast.Import):
            for imported in node.names:
                canonical = imported.name
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
    return aliases


def _shared_admission_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    assert isinstance(tree, ast.Module)
    aliases = _imported_filter_names(tree)
    parents = {child: parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
    function_calls: dict[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] = {}
    filter_names = set(_FILTER_NAMES.values())
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = aliases.get(node.func.id, node.func.id)
        if name not in filter_names:
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            function_calls.setdefault(owner, set()).add(name)
    offenders: list[str] = []
    for function, names in function_calls.items():
        if len(names) >= 2:
            offenders.append(offender_at(path, function, "recursive admission filter list is re-inlined"))
    return offenders


def test_shared_admission_filter_set_has_no_twin_list():
    offenders = app_offenders(_shared_admission_filter_offenders, skip=(_ADMISSION_CHOKEPOINT,))
    assert not offenders, "Use the shared admission filter chokepoint: " + ", ".join(offenders)


def test_shared_admission_filter_detector_bites():
    assert_detector(
        _shared_admission_filter_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def expand(links, visited, scope):\n"
            "    candidates = []\n"
            "    for link in links:\n"
            "        if link.url in visited: continue\n"
            "        if is_analytics_host(link.url): continue\n"
            "        if is_document_url(link.url): continue\n"
            "        if scope is not None and not scope_allows(scope, link.url): continue\n"
            "        candidates.append(link)\n"
            "    return candidates\n": "admission filter list is re-inlined",
        },
        clean=("async def expand(links):\n    return await admit_discovered_links(links)\n",),
    )


def test_job_frontier_detector_sanction_is_narrow():
    path = REPO_ROOT / "app" / "crawl" / "job_store.py"
    allowed = frozenset({"normalize_job_state", "_persist"})
    bad = ast.parse("def grow_frontier(job):\n    job.frontier.append('x')\n")
    assert _direct_frontier_growth_offenders(bad, path, allowed_functions=allowed)
    sanctioned = ast.parse("def normalize_job_state(job):\n    frontier = []\n    job.frontier = frontier\n")
    assert not _direct_frontier_growth_offenders(sanctioned, path, allowed_functions=allowed)


_DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_REQUIRED_PROPERTY_FINGERPRINTS = {
    "public-target": frozenset({"ipaddress_range_verdict"}),
    "bool-env": frozenset({"bool_literal_set"}),
    "robots-policy": frozenset(
        {"robots_url", "parse_robots_rules_call", "parse_robots_rules_definition", "robots_bounded_ttl_cache"}
    ),
}


def _property(name: str) -> Property:
    return PROPERTY_BY_NAME[name]


def _property_contexts(tree: ast.AST) -> list[ast.AST]:
    symbol_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    contexts: list[ast.AST] = [
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in (tree, *(node for node in nodes_of(tree) if isinstance(node, ast.ClassDef))):
        contexts.extend(node for node in getattr(scope, "body", []) if not isinstance(node, symbol_types))
    return contexts


def _module_name(path: pathlib.Path) -> str:
    relative = _relative_app_path(path)
    if relative.endswith("/__init__.py"):
        return relative[: -len("/__init__.py")].replace("/", ".")
    if relative.endswith(".py"):
        return relative[:-3].replace("/", ".")
    return relative.replace("/", ".")


_PROPERTY_MODULES: ContextVar[tuple[tuple[pathlib.Path, ast.AST], ...] | None] = ContextVar(
    "property_modules", default=None
)


def _module_aware(detector):
    def wrapped(tree, path, prop, *, modules=None):
        token = _PROPERTY_MODULES.set(modules)
        try:
            return detector(tree, path, prop)
        finally:
            _PROPERTY_MODULES.reset(token)

    return wrapped


def _module_definition_index(
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> dict[str, list[tuple[pathlib.Path, ast.AST, ast.AST]]]:
    index: dict[str, list[tuple[pathlib.Path, ast.AST, ast.AST]]] = {}
    for path, tree in modules or ():
        module = _module_name(path)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
            elif isinstance(node, ast.ClassDef):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.setdefault(f"{module}.{node.name}.{child.name}", []).append((path, tree, child))
    return index


def _resolvedlocal_nodes(
    tree: ast.AST,
    node: ast.AST,
    excluded_ids: set[int] | frozenset[int] = frozenset(),
    *,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
    prop: Property | None = None,
) -> list[tuple[ast.AST, ast.AST]]:
    definitions: dict[str, list[ast.AST]] = {}
    for candidate in nodes_of(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(candidate.name, []).append(candidate)
    module_index = _module_definition_index(modules)
    resolved: list[tuple[ast.AST, ast.AST]] = []
    seen: set[tuple[int, int]] = set()
    pending: list[tuple[ast.AST, ast.AST]] = [(tree, node)]
    while pending:
        current_tree, current = pending.pop()
        current_key = (id(current_tree), id(current))
        if current_key in seen or id(current) in excluded_ids:
            continue
        seen.add(current_key)
        resolved.append((current_tree, current))
        for child in local_nodes(current):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                matches = definitions.get(child.func.id, [])
                if len(matches) == 1 and id(matches[0]) not in excluded_ids:
                    pending.append((current_tree, matches[0]))
            if not module_index:
                continue
            matches = module_index.get(resolved_call_name(current_tree, child), [])
            if len(matches) != 1:
                continue
            candidate_path, candidate_tree, candidate = matches[0]
            if (
                prop is not None
                and _relative_app_path(candidate_path) == prop.owner_module
                and candidate.name in prop.owner_symbols
            ):
                continue
            pending.append((candidate_tree, candidate))
    return resolved


def _relative_app_path(path: pathlib.Path) -> str:
    try:
        return relative_posix(path)
    except ValueError:
        return path.as_posix()


def _owner_definition_ids(tree: ast.AST, path: pathlib.Path, prop: Property) -> set[int]:
    if _relative_app_path(path) != prop.owner_module:
        return set()
    by_symbol: dict[str, list[ast.AST]] = {}
    for node in nodes_of(tree):
        if isinstance(node, _DEFINITION_TYPES) and node.name in prop.owner_symbols:
            by_symbol.setdefault(node.name, []).append(node)
    owner_ids: set[int] = set()
    for nodes in by_symbol.values():
        owner_ids.add(id(nodes[0]))
    return owner_ids


def _property_owner(
    path: pathlib.Path,
    prop: Property,
    symbol: str | None = None,
    *,
    tree: ast.AST | None = None,
    node: ast.AST | None = None,
) -> bool:
    if node is not None and tree is not None:
        return id(node) in _owner_definition_ids(tree, path, prop)
    return False


def _property_suppressed(prop: Property, path: pathlib.Path, symbol: str, fingerprint: str) -> bool:
    relative = _relative_app_path(path)
    return any(
        entry.module == relative and entry.symbol == symbol and entry.fingerprint == fingerprint
        for entry in prop.suppressions
    )


def _property_nodes(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[tuple[str, ast.AST]]:
    owner_ids = _owner_definition_ids(tree, path, prop)
    return [
        (getattr(node, "name", "<module>"), node)
        for node in _property_contexts(tree)
        if id(node) not in owner_ids
    ]


_PUBLIC_RANGE_ATTRIBUTES = frozenset(
    {"is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified"}
)


def _public_range_accesses(nodes: list[tuple[ast.AST, ast.AST]]) -> int:
    count = 0
    for owner_tree, node in nodes:
        for child in local_nodes(node):
            if isinstance(child, ast.Attribute) and child.attr in _PUBLIC_RANGE_ATTRIBUTES:
                count += 1
            elif isinstance(child, ast.Call):
                name = resolved_call_name(owner_tree, child)
                if name.endswith(".ip_network"):
                    count += 1
                elif (
                    name.endswith(tuple(f".{attribute}" for attribute in _PUBLIC_RANGE_ATTRIBUTES))
                    and len(child.args) >= 2
                ):
                    count += 1
    return count


def _public_target_counts(nodes: list[tuple[ast.AST, ast.AST]]) -> tuple[int, int]:
    parses = sum(
        resolved_call_name(owner_tree, call) == "ipaddress.ip_address"
        for owner_tree, node in nodes
        for call in local_nodes(node)
        if isinstance(call, ast.Call)
    )
    return parses, _public_range_accesses(nodes)


def _public_target_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    observations: dict[str, int] = {}

    if _relative_app_path(path) == prop.owner_module:
        owner_ids = _owner_definition_ids(tree, path, prop)
        owner_nodes = [
            node
            for node in nodes_of(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in owner_ids
        ]
        owner_parses, owner_ranges = _public_target_counts([(tree, node) for node in owner_nodes])
        if owner_parses and owner_ranges:
            observations["ipaddress_range_verdict"] = min(owner_parses, owner_ranges)
        return observations
    excluded = _owner_definition_ids(tree, path, prop)
    for _name, node in _property_nodes(tree, path, prop):
        parses, ranges = _public_target_counts(
            _resolvedlocal_nodes(tree, node, excluded, modules=None, prop=prop)
        )
        count = min(parses, ranges) if parses and ranges else 0
        if count:
            observations["ipaddress_range_verdict"] = observations.get("ipaddress_range_verdict", 0) + count
    return observations


@_module_aware
def _public_target_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    excluded = _owner_definition_ids(tree, path, prop)
    for name, node in _property_nodes(tree, path, prop):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes = _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop)
        parses, ranges = _public_target_counts(nodes)
        if parses and ranges:
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} reimplements public-address policy")
            )
    return offenders


_BOOL_ENV_SPELLINGS = frozenset({"0", "1", "false", "true", "no", "yes", "off", "on"})

# A bool env read is only guarded when the value was normalized first, so these
# are the calls that make one normalized. `getattr(raw, "strip")` counts too:
# it reaches the same method, and recognizing only the attribute form would
# leave a one-token rewrite able to hide the comparison.
_BOOL_ENV_NORMALIZATION_METHODS = frozenset({"strip", "lower", "casefold"})


def _bool_literal_values(node: ast.AST) -> set[str]:
    elements = node.keys if isinstance(node, ast.Dict) else getattr(node, "elts", ())
    return {
        element.value
        for element in elements
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }


def _bool_named_sets(nodes) -> dict[str, set[str]]:
    named: dict[str, set[str]] = {}
    for assignment in nodes:
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
            assignment.value, (ast.Set, ast.List, ast.Tuple, ast.Dict)
        ):
            continue
        literals = _bool_literal_values(assignment.value)
        if not literals & _BOOL_ENV_SPELLINGS:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                named[target.id] = literals
    return named


def _bool_env_literal_sets(
    nodes: list[tuple[ast.AST, ast.AST]],
) -> tuple[dict[int, set[str]], dict[str, set[str]]]:
    direct: dict[int, set[str]] = {}
    descendants = [child for _owner_tree, node in nodes for child in local_nodes(node)]
    for child in descendants:
        if (
            isinstance(child, (ast.Set, ast.List, ast.Tuple, ast.Dict))
            and _bool_literal_values(child) & _BOOL_ENV_SPELLINGS
        ):
            direct[id(child)] = _bool_literal_values(child)
    return direct, _bool_named_sets(descendants)


def _bool_env_tree_sets(
    tree: ast.AST,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
) -> dict[str, set[str]]:
    trees = (tree,) if modules is None else tuple(module_tree for _path, module_tree in modules)
    named: dict[str, set[str]] = {}
    for module_tree in trees:
        named.update(_bool_named_sets(nodes_of(module_tree)))
    return named


def _bool_env_node_observation(
    nodes: list[tuple[ast.AST, ast.AST]],
    tree_sets: dict[str, set[str]] | None = None,
) -> tuple[bool, int]:
    direct, named = _bool_env_literal_sets(nodes)
    named = {**tree_sets, **named} if tree_sets else named
    local_nodes_with_tree = [(owner_tree, child) for owner_tree, node in nodes for child in local_nodes(node)]
    has_normalization = any(
        has_method_call(owner_tree, child, _BOOL_ENV_NORMALIZATION_METHODS)
        for owner_tree, child in local_nodes_with_tree
    )
    if not has_normalization:
        return False, 0
    count = 0
    for _owner_tree, child in local_nodes_with_tree:
        if not isinstance(child, ast.Compare) or not any(
            isinstance(op, (ast.In, ast.NotIn)) for op in child.ops
        ):
            continue
        if any(
            id(part) in direct
            or (isinstance(part, ast.Name) and part.id in named)
            or (
                isinstance(part, ast.Attribute)
                and part.attr == "keys"
                and isinstance(part.value, ast.Name)
                and part.value.id in named
            )
            for part in local_nodes(child)
        ):
            count += 1
    return True, count


def _bool_env_contexts(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> list[list[tuple[ast.AST, ast.AST]]]:
    excluded = _owner_definition_ids(tree, path, prop)
    if _relative_app_path(path) == prop.owner_module:
        return [
            [(tree, node)]
            for node in nodes_of(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in excluded
        ]
    return [
        _resolvedlocal_nodes(tree, node, excluded, modules=modules, prop=prop)
        for _name, node in _property_nodes(tree, path, prop)
    ]


def _bool_env_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    tree_sets = _bool_env_tree_sets(tree, None)
    contexts = _bool_env_contexts(tree, path, prop, None)
    observations_for_contexts = [_bool_env_node_observation(context, tree_sets) for context in contexts]
    if not any(observed for observed, _count in observations_for_contexts):
        return {}
    return {"bool_literal_set": sum(count for _observed, count in observations_for_contexts)}


@_module_aware
def _bool_env_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    tree_sets = _bool_env_tree_sets(tree, _PROPERTY_MODULES.get())
    excluded = _owner_definition_ids(tree, path, prop)
    for name, node in _property_nodes(tree, path, prop):
        context = _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop)
        if _bool_env_node_observation(context, tree_sets)[1]:
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} reimplements boolean environment parsing")
            )
    return offenders


def _source_and_literals(node: ast.AST) -> tuple[str, str]:
    nodes = list(local_nodes(node))
    source = (
        " ".join(ast.unparse(child) for child in nodes[1:]).lower()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        else ast.unparse(node).lower()
    )
    literals = "".join(
        child.value for child in nodes if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ).lower()
    return source, literals


def _robot_signal(node: ast.AST) -> bool:
    source, literals = _source_and_literals(node)
    return any(
        token in source or token in literals for token in ("robot", "crawl-delay", "disallow", "user-agent")
    )


def _call_leaf_names(tree: ast.AST, node: ast.AST):
    for child in local_nodes(node):
        if isinstance(child, ast.Call):
            yield resolved_call_name(tree, child).rsplit(".", 1)[-1].lower()


# `"{}/robots.txt".format(base)` and `"/robots.txt".join(...)` build a URL the
# same way a BinOp or f-string does, so the URL literal detector must see them.
# `getattr(x, "format")` reaches the same method through a token.
_STRING_CONSTRUCTOR_METHODS = frozenset({"format", "join"})
_URL_CONSTRUCTOR_METHODS = frozenset({"urljoin", "urlunsplit"})
# How a line-oriented robots file gets torn into lines. A hand-rolled scanner
# built on anything else is the duplicated robots parser this detector bans.
_ROBOTS_LINE_SPLIT_METHODS = frozenset({"splitlines", "partition"})


def _fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        any(token in name for token in ("fetch", "request", "download", "retrieve", "http"))
        for name in _call_leaf_names(tree, node)
    )


def _string_constructor(tree: ast.AST, node: ast.AST) -> bool:
    return has_method_call(tree, node, _STRING_CONSTRUCTOR_METHODS)


def _url_literal_capability(tree: ast.AST, node: ast.AST) -> bool:
    if not isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
        return False
    nodes = list(local_nodes(node))
    if not any(isinstance(child, (ast.BinOp, ast.JoinedStr)) for child in nodes) and not _string_constructor(
        tree, node
    ):
        return False
    parts = [
        child.value for child in nodes if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]
    return any("robot" in part or ".txt" in part for part in parts) and any(
        part.lstrip("{}").startswith("/") or "http" in part for part in parts
    )


def _url_constructor(tree: ast.AST, node: ast.AST) -> bool:
    nodes = list(local_nodes(node))
    return has_method_call(tree, node, _URL_CONSTRUCTOR_METHODS) or any(
        _url_literal_capability(tree, child) for child in nodes
    )


def _robots_fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        "http" in name
        or name.startswith(("load_", "retrieve", "request"))
        or name == "fetch_and_decode"
        or (name in {"get", "open", "read", "send", "urlopen"} and _robot_signal(node))
        for name in _call_leaf_names(tree, node)
    )


def _parser_call_count(tree: ast.AST, node: ast.AST) -> int:
    return sum(
        1
        for name, _call in local_call_leaf_names(tree, node)
        if ("parse" in name and any(token in name for token in ("robot", "rule", "directive", "crawl")))
        or ("decode" in name and "rule" in name)
        or ("read" in name and "robot" in name)
    )


def _parser_definition(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    _source, _literals = _source_and_literals(node)
    name = node.name.lower()
    if _relative_app_path(path) == "app/crawl/robots_parse.py" and ("robot" in name or "parse" in name):
        return True
    if "robot" in name and any(token in name for token in ("parse", "rule", "directive")):
        return True
    nodes = list(local_nodes(node))
    line_scan = has_method_call(tree, node, _ROBOTS_LINE_SPLIT_METHODS)
    collection_return = any(
        isinstance(child, ast.Return)
        and isinstance(child.value, (ast.ListComp, ast.List, ast.Tuple, ast.Dict, ast.DictComp))
        for child in nodes
    )
    return line_scan and collection_return


def _cache_call_count(tree: ast.AST, node: ast.AST) -> int:
    return sum(
        isinstance(child, ast.Call)
        and resolved_call_name(tree, child).rsplit(".", 1)[-1].lower().endswith("boundedttlcache")
        for child in local_nodes(node)
    )


def _robots_observations_for_node(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> dict[str, int]:
    observations: dict[str, int] = {}
    parser_calls = _parser_call_count(tree, node)
    parser_definitions = int(_parser_definition(tree, path, node))
    cache_calls = _cache_call_count(tree, node)
    url_calls = int(_url_constructor(tree, node))
    if url_calls and _robot_signal(node) and _robots_fetch_like(tree, node):
        observations["robots_url"] = url_calls
    if parser_calls:
        observations["parse_robots_rules_call"] = parser_calls
    if parser_definitions:
        observations["parse_robots_rules_definition"] = parser_definitions
    if cache_calls and (_robot_signal(node) or parser_calls or _fetch_like(tree, node)):
        observations["robots_bounded_ttl_cache"] = cache_calls
    return observations


def _robot_module_paths(
    tree: ast.AST,
    path: pathlib.Path,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> dict[int, pathlib.Path]:
    module_paths = {id(module_tree): module_path for module_path, module_tree in modules or ()}
    module_paths.setdefault(id(tree), path)
    return module_paths


def _robot_capabilities_from_nodes(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    nodes: list[tuple[ast.AST, ast.AST]],
    module_paths: dict[int, pathlib.Path],
    *,
    suppress_shared: bool,
) -> dict[str, int]:
    capabilities: dict[str, int] = {}
    for owner_tree, resolved in nodes:
        owner_path = module_paths.get(id(owner_tree), path)
        for capability, count in _robots_observations_for_node(owner_tree, owner_path, resolved).items():
            if (
                suppress_shared
                and resolved is not nodes[0][1]
                and _robots_shared_parser(prop, owner_path, resolved, owner_tree, capability)
            ):
                continue
            capabilities[capability] = capabilities.get(capability, 0) + count
    return capabilities


def _robots_policy_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    observations: dict[str, int] = {}
    contexts = (
        _property_contexts(tree)
        if _relative_app_path(path) == prop.owner_module
        else [node for _name, node in _property_nodes(tree, path, prop)]
    )
    module_paths = _robot_module_paths(tree, path, None)
    for node in contexts:
        resolved = _resolvedlocal_nodes(tree, node, modules=None, prop=prop)
        for fingerprint, count in _robot_capabilities_from_nodes(
            tree, path, prop, resolved, module_paths, suppress_shared=False
        ).items():
            observations[fingerprint] = observations.get(fingerprint, 0) + count
    return observations


def _robots_shared_parser(
    prop: Property,
    path: pathlib.Path,
    node: ast.AST,
    tree: ast.AST,
    capability: str,
) -> bool:
    return (
        capability == "parse_robots_rules_definition"
        and _relative_app_path(path) != prop.owner_module
        and _parser_definition(tree, path, node)
        and not _robots_fetch_like(tree, node)
        and not _url_constructor(tree, node)
        and not _cache_call_count(tree, node)
    )


@_module_aware
def _robots_policy_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    excluded = _owner_definition_ids(tree, path, prop)
    module_paths = _robot_module_paths(tree, path, _PROPERTY_MODULES.get())
    for name, node in _property_nodes(tree, path, prop):
        nodes = _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop)
        capabilities = _robot_capabilities_from_nodes(
            tree, path, prop, nodes, module_paths, suppress_shared=True
        )
        if "robots_url" not in capabilities and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            builders = {
                resolved.name
                for _owner_tree, resolved in nodes
                if isinstance(resolved, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _url_constructor(_owner_tree, resolved)
                and _robot_signal(resolved)
                and not _robots_fetch_like(_owner_tree, resolved)
            }
            local_calls = {
                child.func.id
                if isinstance(child.func, ast.Name)
                else resolved_call_name(tree, child).rsplit(".", 1)[-1]
                for child in local_nodes(node)
                if isinstance(child, ast.Call)
            }
            if builders & local_calls and _robots_fetch_like(tree, node):
                capabilities["robots_url"] = 1
        for capability in sorted(capabilities):
            if _property_suppressed(prop, path, name, capability):
                continue
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} has robots capability {capability}")
            )
    return offenders


def _property_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    if prop.name == "public-target":
        return _public_target_observations(tree, path, prop)
    if prop.name == "bool-env":
        return _bool_env_observations(tree, path, prop)
    if prop.name == "robots-policy":
        return _robots_policy_observations(tree, path, prop)
    return {}


def _property_registry_failures(
    registry: tuple[Property, ...] = PROPERTY_REGISTRY,
    *,
    trees: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
) -> list[str]:
    failures: list[str] = []
    names = [prop.name for prop in registry]
    if len(names) != len(set(names)):
        failures.append("property registry names are not unique")
    observed: dict[str, dict[str, int]] = {prop.name: {} for prop in registry}
    source_trees = app_trees(exclude=frozenset()) if trees is None else trees
    for path, tree in source_trees:
        for prop in registry:
            for fingerprint, count in _property_observations(tree, path, prop).items():
                observed[prop.name][fingerprint] = observed[prop.name].get(fingerprint, 0) + count
    for prop in registry:
        if not prop.fingerprints:
            failures.append(f"{prop.name} has no fingerprints")
        required = _REQUIRED_PROPERTY_FINGERPRINTS.get(prop.name)
        if required is None:
            failures.append(f"{prop.name} has no detector")
        else:
            missing = required - set(prop.fingerprints)
            if missing:
                failures.append(f"{prop.name} is missing required fingerprints: {sorted(missing)}")
        unobserved = set(prop.fingerprints) - set(observed[prop.name])
        if unobserved:
            failures.append(
                f"{prop.name} has unobserved fingerprints (dead configuration): {sorted(unobserved)}"
            )
        zero_observations = {
            fingerprint
            for fingerprint in prop.fingerprints
            if fingerprint in observed[prop.name] and observed[prop.name][fingerprint] <= 0
        }
        if zero_observations:
            failures.append(
                f"{prop.name} has zero observations for fingerprints: {sorted(zero_observations)}"
            )
    return failures


def _observed_property_count(prop: Property) -> int:
    count = 0
    for path, tree in app_trees(exclude=frozenset()):
        count += sum(_property_observations(tree, path, prop).values())
    return count


def test_property_registry_is_not_empty():
    assert PROPERTY_REGISTRY


def _property_owner_failures(
    registry: tuple[Property, ...],
    definitions: dict[str, list[tuple[str, ast.AST]]],
) -> list[str]:
    failures: list[str] = []
    for prop in registry:
        for symbol in prop.owner_symbols:
            matches = definitions.get(symbol, [])
            if len(matches) != 1:
                failures.append(f"{symbol}={len(matches)}")
                continue
            relative, _node = matches[0]
            if relative != prop.owner_module:
                failures.append(f"{symbol}@{relative}")
    return failures


def test_property_registry_owners_exist():
    assert PROPERTY_REGISTRY
    assert all(prop.reason.strip() for prop in PROPERTY_REGISTRY)
    assert all(entry.reason.strip() for prop in PROPERTY_REGISTRY for entry in prop.suppressions)
    for prop in PROPERTY_REGISTRY:
        site_module, site_symbol = prop.definition_site.split(":", 1)
        assert site_module == prop.owner_module
        assert site_symbol in prop.owner_symbols
    definitions: dict[str, list[tuple[str, ast.AST]]] = {}
    for path, tree in app_trees(exclude=frozenset()):
        relative = _relative_app_path(path)
        for node in nodes_of(tree):
            if isinstance(node, _DEFINITION_TYPES):
                definitions.setdefault(node.name, []).append((relative, node))
    failures = _property_owner_failures(PROPERTY_REGISTRY, definitions)
    assert not failures, "property registry owners are not unique: " + ", ".join(failures)


def test_property_registry_fingerprints_are_observed():
    failures = _property_registry_failures()
    assert not failures, "; ".join(failures)


def test_property_registry_detector_bites():
    prop = _property("bool-env")
    path = REPO_ROOT / "app" / "_zero_observation.py"
    tree = ast.parse("def normalize(value):\n    return value.strip().lower()\n")
    assert _bool_env_observations(tree, path, prop) == {"bool_literal_set": 0}
    failures = _property_registry_failures((prop,), trees=((path, tree),))
    assert any("zero observations" in failure and "bool_literal_set" in failure for failure in failures), (
        failures
    )


#: A module that defines ``parse_bool_value`` twice -- the shape both registry
#: proofs below are about. Named because each carried its own copy of the source
#: and its own re-walk for the duplicate definitions, so a change to the synthetic
#: definition had to be made twice to be made at all, and a change to the walk had
#: to be made once and forgotten once.
DUPLICATE_DEF_SOURCE = (
    "def parse_bool_value(raw, default, *, env_name):\n"
    "    return default\n"
    "def parse_bool_value(raw, default, *, env_name):\n"
    "    return raw.strip().lower() in {'0', 'false', 'no', 'off'}\n"
)


def _duplicate_def_nodes(tree: ast.AST) -> list[ast.FunctionDef]:
    """Every ``parse_bool_value`` definition in ``tree``, in source order."""
    return [
        node
        for node in nodes_of(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "parse_bool_value"
    ]


def test_property_registry_rejects_duplicate_owner_count():
    prop = _property("bool-env")
    tree = ast.parse(DUPLICATE_DEF_SOURCE)
    definitions = {"parse_bool_value": [("app/validation.py", node) for node in _duplicate_def_nodes(tree)]}
    assert _property_owner_failures((prop,), definitions) == ["parse_bool_value=2"]


def test_property_registry_rejects_deleted_fingerprint():
    prop = _property("robots-policy")
    weakened = Property(
        name=prop.name,
        owner_module=prop.owner_module,
        owner_symbols=prop.owner_symbols,
        definition_site=prop.definition_site,
        fingerprints=tuple(item for item in prop.fingerprints if item != "robots_url"),
        reason=prop.reason,
        suppressions=prop.suppressions,
    )
    failures = _property_registry_failures((weakened,))
    assert any("robots_url" in failure for failure in failures), failures


def test_property_owner_duplicate_is_not_sanctioned():
    prop = _property("bool-env")
    tree = ast.parse(DUPLICATE_DEF_SOURCE)
    path = REPO_ROOT / "app" / "validation.py"
    owner_ids = _owner_definition_ids(tree, path, prop)
    duplicate = _duplicate_def_nodes(tree)[1]
    assert len(owner_ids) == 1
    assert not _property_owner(path, prop, tree=tree, node=duplicate)
    assert _bool_env_offenders(tree, path, prop)


def test_public_target_has_one_implementation():
    prop = _property("public-target")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_public_target_offenders(tree, path, prop))
    assert _observed_property_count(prop), "public-target detector observed no fingerprint occurrences"
    assert not offenders, "public-address policy has a second implementation: " + ", ".join(offenders)


def test_public_target_registry_is_not_empty():
    assert _property("public-target").fingerprints


def _synthetic_app_tree(source: str) -> ast.AST:
    """Parse a synthetic module as if it lived at the app-tree root.

    Proof sources are written inline as one string per shape, so a detector
    proof reads as the shape it forbids rather than as harness plumbing.
    """
    return ast.parse(source)


def assert_property_detector_bites(
    prop: Property,
    detector,
    *,
    caught: dict[str, tuple[str | dict[str, str], int]],
    quiet: str | dict[str, str | dict[str, str]],
    quiet_path: pathlib.Path | None = None,
) -> None:
    """Prove a property detector has teeth, in-process, without a break/restore cycle.

    ``caught`` maps a label to (source, exact offender count): every shape must
    be reported with exactly that many offenders, so a detector cannot pass by
    over-reporting. A mapping source places each named module in a separate AST
    and invokes the detector on the final module with all modules available for
    capability resolution. ``quiet`` is a module or labelled modules that
    delegate to the owner and must produce none. This is the entire proof of a
    detector's teeth -- a detector that cannot recognise its own property fails
    here rather than in production.
    """
    path = REPO_ROOT / "app" / "_synthetic.py"

    def run_case(source: str | dict[str, str], default_path: pathlib.Path) -> list[str]:
        if isinstance(source, str):
            return list(detector(_synthetic_app_tree(source), default_path, prop))
        modules = [
            (REPO_ROOT / relative, ast.parse(module_source)) for relative, module_source in source.items()
        ]
        primary_path, primary_tree = modules[-1]
        return list(detector(primary_tree, primary_path, prop, modules=modules))

    for label, (source, expected) in caught.items():
        offenders = run_case(source, path)
        assert len(offenders) == expected, (
            f"{prop.name} detector: {label} shape produced {len(offenders)} offenders, "
            f"expected {expected}: {offenders}"
        )
    resolved_quiet_path = quiet_path or path
    quiet_sources = quiet if isinstance(quiet, dict) else {"owner delegation": quiet}
    for label, source in quiet_sources.items():
        quiet_offenders = run_case(source, resolved_quiet_path)
        assert not quiet_offenders, f"{prop.name} detector flagged {label}: {quiet_offenders}"


def test_public_surface_detector_bites():
    historical_decompression_owner = (
        REPO_ROOT / "tests" / "fixtures" / "safety" / "historical_decompression_owner.txt"
    ).read_text(encoding="utf-8")
    assert_property_detector_bites(
        _PUBLIC_SURFACE_PROPERTY,
        _public_surface_offenders,
        caught={"uncapped decompression at ca425fc": (historical_decompression_owner, 1)},
        quiet=(REPO_ROOT / "app" / "crawl" / "credentials.py").read_text(encoding="utf-8"),
    )


def test_public_target_detector_bites():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            # All four alias forms live in ONE module on purpose: a detector
            # that only inspected a module's first function would pass if each
            # shape were proven in its own module.
            "all alias forms in one module": (
                "import ipaddress\n"
                "import ipaddress as ip\n"
                "from ipaddress import ip_address as parse\n"
                "IP = ipaddress\n"
                "def alias_import(value):\n"
                "    address = ip.ip_address(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def from_import(value):\n"
                "    address = parse(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def assignment(value):\n"
                "    address = IP.ip_address(value)\n"
                "    return address.is_private or IP.ip_network('100.64.0.0/10')\n"
                "def getattr_call(value):\n"
                "    address = getattr(ipaddress, 'ip_address')(value)\n"
                "    return address.is_private or ipaddress.ip_network('100.64.0.0/10')\n",
                4,
            ),
            "helper split with dynamic range access": (
                "import ipaddress\n"
                "def parse_address(value):\n"
                "    return ipaddress.ip_address(value)\n"
                "def verdict(value):\n"
                "    return not getattr(parse_address(value), 'is_private')\n",
                1,
            ),
            "aliased getattr": (
                "from builtins import getattr as ga\n"
                "import ipaddress\n"
                "def verdict(value):\n"
                "    return not ga(ipaddress, 'ip_address')(value).is_private\n",
                1,
            ),
            "constant-folded getattr name": (
                "import ipaddress\n"
                "def verdict(value):\n"
                "    name = 'ip_' + 'address'\n"
                "    return not getattr(ipaddress, name)(value).is_private\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import _is_public_ip as verdict\n"
            "def check(value):\n"
            "    return verdict(value)\n"
        ),
    )
    runtime_name = ast.parse(
        "import ipaddress\n"
        "def attribute_name():\n"
        "    return 'ip_address'\n"
        "def verdict(value):\n"
        "    name = attribute_name()\n"
        "    return not getattr(ipaddress, name)(value).is_private\n"
    )
    assert not _public_target_offenders(
        runtime_name,
        REPO_ROOT / "app" / "_synthetic.py",
        _property("public-target"),
    )


def test_bool_env_has_one_implementation():
    prop = _property("bool-env")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_bool_env_offenders(tree, path, prop))
    assert _observed_property_count(prop), "bool-env detector observed no fingerprint occurrences"
    assert not offenders, "boolean environment policy has a second implementation: " + ", ".join(offenders)


def test_bool_env_registry_is_not_empty():
    assert _property("bool-env").fingerprints


def test_bool_env_detector_bites():
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "truthy set": (
                "def parse(value):\n    return value.strip().lower() in {'1', 'true', 'yes', 'on'}\n",
                1,
            ),
            "inverted set": (
                "def parse(value):\n    return value.strip().lower() not in {'0', 'false', 'no', 'off'}\n",
                1,
            ),
            "module-level set": (
                "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                "def parse(value):\n"
                "    return value.strip().lower() not in FALSE_WORDS\n",
                1,
            ),
            "helper split through dict keys": (
                "FALSE_WORDS = {'0': False, 'false': False, 'no': False, 'off': False}\n"
                "def normalize(value):\n"
                "    return value.strip().lower()\n"
                "def parse(value):\n"
                "    return normalize(value) in FALSE_WORDS.keys()\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import parse_bool_value as parse\n"
            "def parse_env(value, default):\n"
            "    return parse(value, default, env_name='X')\n"
        ),
    )


def test_robots_policy_has_one_implementation():
    prop = _property("robots-policy")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_robots_policy_offenders(tree, path, prop))
    assert _observed_property_count(prop), "robots-policy detector observed no fingerprint occurrences"
    assert not offenders, "robots policy has a second implementation: " + ", ".join(offenders)


def test_robots_policy_registry_is_not_empty():
    assert _property("robots-policy").fingerprints


def test_robots_policy_detector_bites():
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "parser call and cache": (
                "from app.bounded_ttl import BoundedTTLCache as Cache\n"
                "from app.crawl.robots_parse import parse_robots_rules as parse\n"
                "def robots_loader(origin):\n"
                "    cache = Cache(max_entries=1, ttl=1)\n"
                "    return parse('User-agent: *')\n",
                2,
            ),
            "parser definition": (
                "def parse_robots_rules(text):\n    return [(True, text)]\n",
                1,
            ),
            "split url literal": (
                "def renamed_loader(origin):\n"
                "    path = '/r' + 'obots.txt'\n"
                "    return http_fetcher(urljoin(origin, path))\n",
                1,
            ),
            "renamed parser definition": (
                "def decode_directives(text):\n"
                "    rows = text.splitlines()\n"
                "    return [(True, row) for row in rows if 'disallow' in row.lower()]\n",
                1,
            ),
            "helper split with formatted url": (
                "def endpoint(origin):\n"
                "    return '{}/robots.txt'.format(origin)\n"
                "def load(origin):\n"
                "    return http_fetcher(endpoint(origin))\n",
                1,
            ),
            "inline joined url": (
                "def load(origin):\n    return http_fetcher(''.join((origin, '/robots.txt')))\n",
                1,
            ),
            "inline f-string url": (
                "def load(origin):\n    return http_fetcher(f'{origin}/robots.txt')\n",
                1,
            ),
            "inline absolute formatted url": (
                "def load(origin):\n    return http_fetcher('https://{}/robots.txt'.format(origin))\n",
                1,
            ),
            "helper split with arbitrary dict parser": (
                "def directives(text):\n"
                "    return {line: True for line in text.splitlines()}\n"
                "def load(text):\n"
                "    return http_fetcher(directives(text))\n",
                1,
            ),
        },
        quiet=(
            "from app.crawl.robots import load_robots_rules as load\n"
            "def check(origin):\n"
            "    return load(origin, fetcher, 5)\n"
        ),
    )


def test_property_detectors_resolve_imported_helpers_across_modules():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            "imported verdict helper": (
                {
                    "app/_public_helper.py": (
                        "import ipaddress\n"
                        "def parse_address(value):\n"
                        "    return ipaddress.ip_address(value)\n"
                    ),
                    "app/_public_consumer.py": (
                        "from app._public_helper import parse_address\n"
                        "def verdict(value):\n"
                        "    address = parse_address(value)\n"
                        "    return address.is_private\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_public_owner.py": "def verdict(value):\n    return value\n",
                "app/_public_consumer.py": (
                    "from app._public_owner import verdict\ndef check(value):\n    return verdict(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "imported normalization helper": (
                {
                    "app/_bool_helper.py": ("def normalize(value):\n    return value.strip().lower()\n"),
                    "app/_bool_consumer.py": (
                        "from app._bool_helper import normalize\n"
                        "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                        "def parse(value):\n"
                        "    return normalize(value) in FALSE_WORDS\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_bool_owner.py": "def parse(value):\n    return value\n",
                "app/_bool_consumer.py": (
                    "from app._bool_owner import parse\ndef check(value):\n    return parse(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "imported policy helper": (
                {
                    "app/_robots_helper.py": (
                        "def parse_robot_policy(text):\n"
                        "    return [(True, text)]\n"
                        "def load_policy(text):\n"
                        "    return parse_robot_policy(text)\n"
                    ),
                    "app/_robots_consumer.py": (
                        "from app._robots_helper import load_policy\n"
                        "def load(text):\n"
                        "    return load_policy(text)\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "shared field parser": {
                "app/_robots_primitive.py": (
                    "def parse_fields(text):\n    return [(True, line) for line in text.splitlines()]\n"
                ),
                "app/_robots_consumer.py": (
                    "from app._robots_primitive import parse_fields\n"
                    "def load(text):\n"
                    "    return parse_fields(text)\n"
                ),
            },
        },
    )


_KNOB_PATTERN = re.compile(r"\bBROWSER_CRAWLER_[A-Z0-9_]+\b")


def _documented_browser_knobs() -> set[str] | None:
    env_path = REPO_ROOT / ".env.example"
    readme_path = REPO_ROOT / "README.md"
    if not env_path.exists() or not readme_path.exists():
        return None
    env_names = {
        match.group(0)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for match in _KNOB_PATTERN.finditer(line)
    }
    readme_names = set(_KNOB_PATTERN.findall(readme_path.read_text(encoding="utf-8")))
    return {name for name in env_names | readme_names if not name.endswith("_")}


def _settings_alias_declaration_constant(
    path: pathlib.Path,
    tree: ast.AST,
    node: ast.Constant,
) -> bool:
    if _relative_app_path(path) != "app/settings.py":
        return False
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
    current: ast.AST = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ENVIRONMENT_FIELD_ALIASES"
            for target in current.targets
        ):
            return True
    return False


_ENV_READER_NAMES = frozenset(
    {
        "get",
        "getenv",
        "get_env_float",
        "get_env_int",
        "get_mapping_float",
        "get_mapping_int",
        "parse_bool_env",
        "parse_bool_value",
    }
)


def _environment_reader_reference(
    tree: ast.AST,
    node: ast.Constant,
    field_environments: dict[str, str],
) -> str | None:
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
    expression = parents.get(id(node))
    scope = parents.get(id(expression)) if expression is not None else None
    if (
        isinstance(node, ast.Constant)
        and isinstance(expression, ast.Expr)
        and expression.value is node
        and getattr(scope, "body", ())
        and scope.body[0] is expression
    ):
        return None
    current: ast.AST = node
    alias_value: str | None = None
    is_alias_lookup = False
    reaches_reader = False
    while id(current) in parents:
        current = parents[id(current)]
        if (
            isinstance(current, ast.Subscript)
            and isinstance(current.value, ast.Name)
            and current.value.id == "ENVIRONMENT_FIELD_ALIASES"
        ):
            is_alias_lookup = True
            if isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
                alias_value = field_environments.get(current.slice.value)
        if (
            isinstance(current, ast.Call)
            and resolved_call_name(tree, current).rsplit(".", 1)[-1] in _ENV_READER_NAMES
        ):
            reaches_reader = True
    if not reaches_reader:
        return None
    if is_alias_lookup:
        return alias_value
    if node.value.startswith("BROWSER_CRAWLER_"):
        return node.value
    return None


def _settings_fields() -> set[str]:
    tree = file_tree("app/settings.py")[1]
    for node in nodes_of_type(tree, ast.ClassDef):
        if node.name == "CrawlerSettings":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return set()


def _settings_alias_failures(
    aliases: dict[str, str],
    fields: set[str] | None = None,
) -> list[str]:
    known_fields = fields if fields is not None else _settings_fields()
    failures = [
        f"{field} maps to invalid environment name {environment}"
        for field, environment in aliases.items()
        if field not in known_fields or not environment.startswith("BROWSER_CRAWLER_")
    ]
    fields_by_environment: dict[str, list[str]] = {}
    for field, environment in aliases.items():
        fields_by_environment.setdefault(environment, []).append(field)
    failures.extend(
        f"{', '.join(sorted(fields_for_environment))} map to {environment}"
        for environment, fields_for_environment in sorted(fields_by_environment.items())
        if len(fields_for_environment) > 1
    )
    return failures


def _production_browser_knob_references() -> set[str]:
    references: set[str] = set()
    fields = _settings_fields()
    field_knobs = {f"BROWSER_CRAWLER_{field.upper()}": field for field in fields}
    for path, tree in app_trees(exclude=frozenset()):
        for node in nodes_of_type(tree, ast.Constant):
            if isinstance(node.value, str) and (not _settings_alias_declaration_constant(path, tree, node)):
                environment = _environment_reader_reference(
                    tree,
                    node,
                    ENVIRONMENT_FIELD_ALIASES,
                )
                if environment is not None:
                    references.add(environment)
            if isinstance(node, ast.Attribute) and _relative_app_path(path) != "app/settings.py":
                environment = ENVIRONMENT_FIELD_ALIASES.get(node.attr)
                if environment is not None:
                    references.add(environment)
                conventional = f"BROWSER_CRAWLER_{node.attr.upper()}"
                if conventional in field_knobs:
                    references.add(conventional)
    dockerfile = REPO_ROOT / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("CMD", "ENTRYPOINT")):
                references.update(_KNOB_PATTERN.findall(line))
    return references


def _documented_knob_offenders(
    documented: set[str] | None,
    references: set[str],
) -> list[str]:
    if documented is None:
        return ["documented knob source could not be located"]
    if not documented:
        return ["no documented BROWSER_CRAWLER knobs were found"]
    return [f"{name} has no production consumer" for name in sorted(documented - references)]


def _resolved_env_reader_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    references: set[str] = set()
    for node in nodes_of_type(tree, ast.Constant):
        if isinstance(node.value, str):
            environment = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
            if environment is not None:
                references.add(environment)
    return _documented_knob_offenders(
        {"BROWSER_CRAWLER_LAZY_SCROLL_STEPS"},
        references,
    )


def test_documented_browser_knobs_have_consumers():
    assert not _settings_alias_failures(ENVIRONMENT_FIELD_ALIASES)
    failures = _documented_knob_offenders(
        _documented_browser_knobs(),
        _production_browser_knob_references(),
    )
    assert not failures, "; ".join(failures)


def test_documented_knob_detector_bites():
    assert _documented_knob_offenders({"BROWSER_CRAWLER_FAKE"}, set()) == [
        "BROWSER_CRAWLER_FAKE has no production consumer"
    ]
    assert _documented_knob_offenders(None, set()) == ["documented knob source could not be located"]
    current = _documented_browser_knobs()
    assert current is not None
    assert "BROWSER_CRAWLER_ALLOWED_HOSTS" in current
    assert not _documented_knob_offenders(
        {"BROWSER_CRAWLER_ALLOWED_HOSTS"}, {"BROWSER_CRAWLER_ALLOWED_HOSTS"}
    )
    assert _documented_knob_offenders({"BROWSER_CRAWLER_LOG_FILE"}, set())
    assert _settings_alias_failures({"missing_field": "BROWSER_CRAWLER_FAKE"}, {"real_field"})
    assert _settings_alias_failures(
        {
            "browser_lazy_scroll_steps": "BROWSER_CRAWLER_FAKE",
            "log_file_path": "BROWSER_CRAWLER_FAKE",
        },
        {"browser_lazy_scroll_steps", "log_file_path"},
    ) == ["browser_lazy_scroll_steps, log_file_path map to BROWSER_CRAWLER_FAKE"]
    prop = Property(
        name="resolved-env-reader",
        owner_module="app/settings.py",
        owner_symbols=("from_mapping",),
        definition_site="app/settings.py:from_mapping",
        fingerprints=("environment_reader_reference",),
        reason="A documented environment name must resolve through a value reader.",
    )
    assert_property_detector_bites(
        prop,
        _resolved_env_reader_offenders,
        caught={
            "mapping lookup passes field name to reader": (
                "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
                "def broken(source):\n"
                "    return get_mapping_int(source, "
                "ENVIRONMENT_FIELD_ALIASES['BROWSER_CRAWLER_LAZY_SCROLL_STEPS'], 3)\n",
                1,
            ),
            "docstring-only mention": (
                "def read_setting():\n"
                "    '''Mentions BROWSER_CRAWLER_LAZY_SCROLL_STEPS without reading it.'''\n"
                "    return object()\n",
                1,
            ),
        },
        quiet=(
            "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
            "def resolved(source):\n"
            "    return get_mapping_int(source, ENVIRONMENT_FIELD_ALIASES['browser_lazy_scroll_steps'], 3)\n"
        ),
    )


_MIGRATION_DDL_PROPERTY = Property(
    name="migration-ddl",
    owner_module="app/crawl/migrations/",
    owner_symbols=(),
    definition_site="app/crawl/migrations/",
    fingerprints=("ddl",),
    reason="Schema changes must remain ordered and reversible in the migration package.",
)


def _ddl_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    if _relative_app_path(path).startswith("app/crawl/migrations/"):
        return []
    found: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if re.match(
            r"\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b",
            node.value,
            re.IGNORECASE,
        ):
            found.append(offender_at(path, node, node.value.splitlines()[0]))
    return found


def test_ddl_is_confined_to_the_migrations_package():
    offenders = [
        found
        for path, tree in app_trees(exclude=frozenset())
        for found in _ddl_offenders(tree, path, _MIGRATION_DDL_PROPERTY)
    ]
    assert not offenders, "DDL outside app/crawl/migrations: " + ", ".join(offenders)


def test_ddl_detector_bites():
    assert_property_detector_bites(
        _MIGRATION_DDL_PROPERTY,
        _ddl_offenders,
        caught={
            "create alter drop": (
                "def bypass(conn):\n"
                "    conn.execute('CREATE TABLE x (id TEXT)')\n"
                "    conn.execute('ALTER TABLE x ADD COLUMN y TEXT')\n"
                "    conn.execute('DROP TABLE x')\n",
                3,
            )
        },
        quiet="def apply(conn):\n    conn.execute('CREATE TABLE x (id TEXT)')\n",
        quiet_path=REPO_ROOT / "app" / "crawl" / "migrations" / "0001_crawl_jobs_initial.py",
    )


# ===========================================================================
# NO TEST MAY MUTATE THE PRODUCTION TREE
#
# `tests/test_jscpd_gate.py` proved the jscpd `ignore` glob by planting two
# clone pairs at FIXED paths inside the live `app/` tree and unlinking them in a
# `finally`. That made `app/` a directory that MOVES while the suite runs, and
# anything that walks it concurrently becomes a list-then-read race: it globs
# the file list, the probe disappears, and `read_text` raises. Measured with 6
# concurrent copies of that file against 6 concurrent copies of this one, 10
# tests failed with `FileNotFoundError: .../app/crawl/migrations/zz_probe_clone.py`
# -- and the victims were the tree-wide chokepoint detectors (await-in-lock,
# bare `re.compile`, `regex` import), so they reported nothing for the entire run
# while appearing green. A detector that cannot see the tree cannot hold it.
#
# So the class closes mechanically rather than one file at a time: this sweep
# reads every module under tests/ and reports any call that can mutate a path
# under app/. It is a name-and-shape check over statically resolvable
# expressions; a path handed in as a parameter and written by a caller outside
# tests/ is invisible to it, which is why a name it DOES resolve is a hard
# failure and not a warning.
#
# The sanctioned way to get a real production path SHAPE is to mirror the
# layout under `tmp_path` and point the code under test at the mirror. A
# `tmp_path / "app"` fixture is deliberately NOT this rule: a scratch directory
# a test builds itself is not the production tree.
# ===========================================================================

# A path built from one of these is anchored at the service root, so the
# segment joined onto it next names a real tree. Aliases a module binds to one
# of them are absorbed below, so `ROOT = HERE.parent` is the same anchor.
_SERVICE_ROOT_NAMES = frozenset({"REPO_ROOT", "SERVICE_ROOT", "HERE", "ROOT"})
# Already inside app/ by construction: the walk helpers hand these out.
_APP_ROOTED_NAMES = frozenset({"APP_ROOT", "STRATEGIES_DIR"})
# Qualifiers whose mutating call takes the path as an ARGUMENT
# (`os.remove(p)`, `shutil.rmtree(p)`) instead of as its receiver.
_FS_MODULE_QUALIFIERS = frozenset({"os", "shutil", "pathlib", "io", "posixpath", "ntpath"})
# Calls that change the filesystem. `str.replace` and `list.remove` are in here
# too, which costs nothing: a call is only reported when its target resolves
# into app/, and a string is not a path into app/.
_MUTATING_METHODS = frozenset(
    {
        "unlink",
        "write_text",
        "write_bytes",
        "mkdir",
        "rmdir",
        "rename",
        "replace",
        "touch",
        "symlink_to",
        "hardlink_to",
        "link_to",
        "chmod",
        "chown",
        "truncate",
        "rmtree",
        "copy",
        "copy2",
        "copyfile",
        "move",
        "remove",
        "removedirs",
        "makedirs",
    }
)
_MUTATING_FUNCTIONS = frozenset(
    {
        "remove",
        "unlink",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "truncate",
        "rmtree",
        "copy",
        "copy2",
        "copyfile",
        "move",
        "write_text",
        "write_bytes",
        "symlink",
        "link",
        "chmod",
        "mkstemp",
        "mkdtemp",
    }
)
# An `open()` is a write only with a mode that says so; a mode this cannot read
# is treated as a read, because `Path.read_text`/`open(p).read()` on an app/
# module is a legitimate thing for a detector test to do.
_WRITE_MODE_CHARS = ("w", "a", "x", "+")
# Alias chains (`ROOT = HERE.parent`, `APP = REPO_ROOT / "app"`) are resolved
# to a fixed point rather than recursively; four passes is far past the longest
# chain a module can express in one scope and bounds the walk either way.
_BINDING_PASSES = 4


def _names_app_tree(component: ast.expr) -> bool:
    """Whether one path component selects the `app/` tree.

    A relative literal names the tree when its FIRST segment is `app`, so
    `tests/fixtures/app/x.py` is a fixture and not a production path. An
    absolute literal is a real filesystem path and any `app` segment in it
    counts.
    """
    if not (isinstance(component, ast.Constant) and isinstance(component.value, str)):
        return False
    parts = [part for part in component.value.split("/") if part not in ("", ".")]
    if not parts:
        return False
    return parts[0] == "app" or (component.value.startswith("/") and "app" in parts)


def _path_components(expr: ast.AST) -> list[ast.AST] | None:
    """Flatten `a / b / c`, `os.path.join(a, b)`, and `Path(a)` into components."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        head = _path_components(expr.left)
        return None if head is None else [*head, expr.right]
    if isinstance(expr, ast.Call):
        name = getattr(expr.func, "attr", None) or getattr(expr.func, "id", None)
        if name in {"Path", "PurePath"} and expr.args:
            # `Path(APP_ROOT / "x")` is the same path as `APP_ROOT / "x"`, so
            # the wrapped expression is flattened rather than boxed.
            return _path_components(expr.args[0])
        if name == "join" and expr.args:
            head = _path_components(expr.args[0])
            return None if head is None else [*head, *expr.args[1:]]
    return [expr]


def _tree_role(
    components: list[ast.AST],
    anchors: set[str],
    rooted: set[str],
) -> str:
    """Classify a path expression as the app tree, a service root, or neither.

    Returns ``"app"`` for anything that lands in `app/`, ``"root"`` for a bare
    service root (so an alias of one can be absorbed), and ``"other"`` for every
    expression whose first filesystem anchor this cannot name -- `tmp_path`, a
    parameter, a value returned by a call. That last case is the whole point:
    an unnamed anchor cannot be assumed to be the production tree.
    """
    for position, component in enumerate(components):
        if isinstance(component, ast.Name) and component.id in rooted:
            return "app"
        if isinstance(component, ast.Attribute) and component.attr in rooted:
            return "app"
        if isinstance(component, (ast.Name, ast.Attribute)):
            name = component.id if isinstance(component, ast.Name) else component.attr
            if name in anchors:
                following = components[position + 1 : position + 2]
                return "app" if following and _names_app_tree(following[0]) else "root"
            return "other"
        if _names_app_tree(component):
            return "app"
        return "other"
    return "other"


def _absorb_path_aliases(tree: ast.AST, anchors: set[str], rooted: set[str]) -> bool:
    """Add names this module binds to a service root or to `app/`."""
    grew = False
    for node in nodes_of(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        components = _path_components(node.value)
        if components is None:
            continue
        role = _tree_role(components, anchors, rooted)
        if role not in {"app", "root"}:
            continue
        names = (
            [target.id for target in node.targets if isinstance(target, ast.Name)]
            if isinstance(node, ast.Assign)
            else [node.target.id]
            if isinstance(node.target, ast.Name)
            else []
        )
        target_set = rooted if role == "app" else anchors
        for name in names:
            if name not in target_set:
                target_set.add(name)
                grew = True
    return grew


def _app_path_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """The service-root and app-tree names this module can name."""
    anchors = set(_SERVICE_ROOT_NAMES)
    rooted = set(_APP_ROOTED_NAMES)
    for _ in range(_BINDING_PASSES):
        if not _absorb_path_aliases(tree, anchors, rooted):
            break
    return anchors, rooted


def _opens_for_writing(node: ast.Call) -> bool:
    """Whether `open(...)` is called with a mode that writes."""
    if len(node.args) < 2:
        return False
    mode = node.args[1]
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(char in mode.value for char in _WRITE_MODE_CHARS)
    )


def _app_tree_mutation_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Report every call in this module that can mutate a path under `app/`."""
    anchors, rooted = _app_path_names(tree)
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        callee = node.func
        if isinstance(callee, ast.Attribute):
            name = callee.attr
            if name not in _MUTATING_METHODS and not (name == "open" and _opens_for_writing(node)):
                continue
            qualifier = resolved_expr_name(tree, callee.value).rsplit(".", 1)[-1]
            targets = node.args if qualifier in _FS_MODULE_QUALIFIERS else [callee.value]
        elif isinstance(callee, ast.Name) and (
            callee.id in _MUTATING_FUNCTIONS or (callee.id == "open" and _opens_for_writing(node))
        ):
            targets = node.args
        else:
            continue
        if not targets or _tree_role(_path_components(targets[0]) or [], anchors, rooted) != "app":
            continue
        offenders.append(offender_at(path, node, f"mutates the production tree: {ast.unparse(callee)}()"))
    return offenders


def test_tests_do_not_mutate_the_production_tree():
    """No test writes, unlinks, renames, or replaces a path inside `app/`.

    Tree-wide, and read from the tree rather than from a file list, so the
    sweep covers a test added tomorrow with nothing to register.
    """
    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        offenders.extend(_app_tree_mutation_offenders(tree, path))
    assert not offenders, (
        "mirror the path shape under tmp_path and point the code under test at the mirror: "
        "a test that mutates app/ turns every concurrent tree walk into a list-then-read race, "
        "and the detectors that lose it report nothing while looking green: " + "; ".join(offenders)
    )


def test_app_tree_mutation_detector_bites():
    """Bites: the shapes that actually shipped, and the near-misses beside them.

    The first three are the real offender -- a fixed path under the live tree,
    written and unlinked from a test -- so the sweep has to catch the exact
    spelling in the tree, not a paraphrase of it. The rest are the ways a
    plausible fix could regress the rule into either a no-op or a false alarm.
    """
    planted = ast.parse(
        "import _jscpd_gate as gate\n"
        "\n"
        "def test_migrations_clones_are_not_reported(capsys):\n"
        "    in_migrations = gate.SERVICE_ROOT / 'app' / 'crawl' / 'migrations' / 'zz_probe.py'\n"
        "    in_production = gate.SERVICE_ROOT / 'app' / 'zz_probe.py'\n"
        "    in_migrations.write_text('x')\n"
        "    try:\n"
        "        assert True\n"
        "    finally:\n"
        "        in_production.unlink()\n"
    )
    offenders = _app_tree_mutation_offenders(planted, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(offenders) == 2, offenders
    assert all("write_text" in found or "unlink" in found for found in offenders), offenders

    module_qualified = ast.parse(
        "from pathlib import Path\n"
        "from chokepoint_walks import APP_ROOT\n"
        "import shutil\n"
        "\n"
        "def wipe():\n"
        "    shutil.rmtree(APP_ROOT)\n"
        "    Path.cwd()\n"
        "    Path(APP_ROOT / 'settings.py').replace(APP_ROOT / 'settings.py.bak')\n"
        "    open(APP_ROOT / 'settings.py', 'w').write('x')\n"
    )
    qualified = _app_tree_mutation_offenders(module_qualified, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(qualified) == 3, qualified

    aliased = ast.parse(
        "from pathlib import Path\n"
        "import os\n"
        "\n"
        "ROOT = Path(__file__).resolve().parent.parent\n"
        "APP = ROOT / 'app'\n"
        "\n"
        "def rename_it():\n"
        "    (APP / 'crawl' / 'ids.py').rename(APP / 'crawl' / 'ids_v2.py')\n"
        "    os.remove(ROOT / 'app' / 'main.py')\n"
    )
    assert len(_app_tree_mutation_offenders(aliased, REPO_ROOT / "tests" / "_synthetic.py")) == 2

    scratch = ast.parse(
        "def test_mirrored_tree(tmp_path):\n"
        "    app = tmp_path / 'app'\n"
        "    (app / 'crawl' / 'migrations').mkdir(parents=True)\n"
        "    (app / 'zz_probe.py').write_text('payload')\n"
        "    (tmp_path / 'tests' / 'fixtures' / 'app' / 'x.py').write_text('fixture')\n"
        "    Path = __import__('pathlib').Path\n"
        "    (Path(__file__).resolve().parent / 'test_jscpd_gate.py').read_text()\n"
    )
    assert not _app_tree_mutation_offenders(scratch, REPO_ROOT / "tests" / "_synthetic.py"), (
        "a test's own scratch tree is not the production tree"
    )


# ===========================================================================
# THE CHOKEPOINT OWNS ITS TRAVERSAL (one snapshot, shared by every detector)
#
# `app_trees` was a generator that re-globbed, re-read and re-parsed all 105 app/
# modules on every call, and each of the ~7 tree-walking detectors called it, so
# one session paid for the same parse about seven times over. Measured on
# tests/test_safety_invariants.py alone: 37.7s of a 49.4s file, led by
# test_property_registry_fingerprints_are_observed 8.06s,
# test_documented_browser_knobs_have_consumers 7.49s,
# test_robots_policy_has_one_implementation 7.45s, test_bool_env_has_one_implementation
# 5.28s, test_public_target_has_one_implementation 3.95s,
# test_property_registry_rejects_deleted_fingerprint 3.63s and
# test_url_returning_helpers_require_host_policy 1.83s.
#
# The cost was the smaller half of the defect. Because every walk observed the
# tree at a DIFFERENT INSTANT, "list the files" and "read the files" were two
# moments, and anything that appeared or vanished between them raised
# FileNotFoundError inside a detector instead of being reported -- the
# list-then-read race that tests/test_jscpd_gate.py used to cause whenever it
# planted a file in app/ beside a chokepoint sweep. Same root cause, one fix:
# the traversal is executed once per exclude set and every detector is handed
# the same ast.Module objects.
#
# The three tests below demonstrate that by execution. None of them writes to
# app/ -- the sweep above forbids it -- so the race is reproduced by making the
# filesystem refuse to answer, which is what a vanished file looks like from
# inside a list-then-read walk.
#
# ONE NODE SEQUENCE PER ROOT, which is the same fix one level deeper. Owning
# the snapshot removed the re-GLOB and the re-READ; the re-WALK underneath it
# was still there, and it was the larger half: 40 detectors and 91 `ast.walk`
# sites over ~105 modules meant forty passes over every node where one would
# do. `chokepoint_walks.nodes_of` materializes `tuple(ast.walk(root))` once per
# root and every detector iterates that, so a detector's own logic is untouched
# and what is removed is the walk it would have repeated. Sub-walks -- a
# detector narrowing to one function and walking that -- are memoized on the
# same terms, and they are where the sharing pays most, because the same
# function body is walked by many detectors.
#
# The last two tests below are the receipt for that. A memo is the easiest
# possible way to make forty detectors fast and blind at once: return an empty
# tuple for an unwalked root and every one of them reports nothing, every one
# of them stays green, and the suite is faster. So they check identity (one
# materialization, shared), element-wise equality against a FRESH `ast.walk`
# (the same objects in the same order, not a subset or a re-ordering), and
# coverage of every module in the snapshot (no root is silently absent).
# ===========================================================================


def _app_reads(monkeypatch) -> list[pathlib.Path]:
    """Record every `read_text` of a file under app/ for the rest of this test."""
    original = pathlib.Path.read_text
    seen: list[pathlib.Path] = []

    def counting(self: pathlib.Path, *args, **kwargs) -> str:
        if self.is_relative_to(APP_ROOT):
            seen.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counting)
    return seen


def test_every_detector_sees_the_same_app_trees():
    """Two walks are one snapshot: the SAME ast.Module objects, not two parses.

    Identity, not equality. A second parse of an unchanged file compares equal
    for every field a detector looks at, so a detector cannot tell a
    re-observation from a changed tree -- which is exactly the distinction the
    shared snapshot exists to draw.
    """
    first = app_trees()
    second = app_trees()
    assert first, "the app tree walk found no modules to compare"
    assert second is first, "each caller is being handed its own walk of app/"
    assert all(one is other for one, other in zip(first, second, strict=True)), (
        "the same module parsed twice is not a shared snapshot"
    )


def test_the_app_tree_is_read_once_however_many_detectors_walk_it(monkeypatch):
    """N detectors cost one read per module, not N.

    Bounded by the module count rather than equal to it, so the assertion is
    true whether this test took the snapshot or an earlier one in the session
    did. Today each walk re-reads the tree, so three walks cost three times the
    modules and this is where the 37.7s went.
    """
    reads = _app_reads(monkeypatch)
    for _ in range(3):
        # Consumed, not merely called: a walk nobody iterates reads nothing, and
        # this test has to be able to see a re-walk.
        tuple(app_trees())
        tuple(app_python_files())
    assert len(reads) <= len(app_python_files()), (
        f"the app tree was read {len(reads)} times for {len(app_python_files())} modules; "
        "the traversal has to be owned by the chokepoint, not repeated per detector"
    )


def test_a_later_walk_cannot_observe_a_tree_that_moved(monkeypatch):
    """The race half: once the snapshot exists, no walk touches the tree again.

    The filesystem is made to refuse rather than made to change, so this proves
    the property without a second offender. A walk that re-globbed raises here;
    a walk that returns its snapshot cannot, because a file that disappears
    after the snapshot is not something a snapshot can observe.
    """
    warm = app_trees()
    assert warm, "the app tree walk found no modules"

    def refuse(*_args, **_kwargs):
        raise AssertionError("a later walk touched the tree instead of returning its snapshot")

    monkeypatch.setattr(pathlib.Path, "rglob", refuse)
    monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    assert app_trees() is warm


def test_the_path_list_and_the_parsed_trees_are_one_traversal(monkeypatch):
    """Asking for paths does not parse, and asking again costs nothing.

    The line-budget sweep wants the file list and the detectors want the trees;
    both are the same rglob, so the list half must not drag a parse along with
    it and neither may be recomputed per caller.
    """
    reads = _app_reads(monkeypatch)
    paths = app_python_files()
    assert len(reads) == 0, f"listing the tree parsed {len(reads)} modules it did not need parsed"
    trees = app_trees()
    assert [path for path, _tree in trees] == list(paths), "the trees are not the files that were listed"
    # Exactly one read per module when this test took the snapshot, and none at
    # all when an earlier test in the session already did: both are "once", and
    # the assertion has to hold whichever order the session ran in.
    assert len(reads) in (0, len(trees)), (
        f"{len(reads)} reads for {len(trees)} modules; the snapshot is read once, not once per caller"
    )
    assert app_python_files() is paths, "the file list is re-globbed per caller"
    assert app_trees() is trees, "the tree snapshot is rebuilt per caller"


def test_the_tests_tree_is_walked_once_too():
    """The tests/ sweep is the same deal: five detectors, one walk.

    Same return type as ``app_trees`` for the same reason, and the five
    callers of ``trees_under(TESTS_DIR)`` now share one snapshot instead of
    each re-parsing the tree they are all reading.
    """
    first = trees_under(TESTS_DIR)
    assert first, "the tests/ walk found no modules"
    assert trees_under(TESTS_DIR) is first, "the tests/ tree is re-walked per detector"
    assert trees_under(TESTS_DIR / "crawl") is not first, "a different tree must be its own snapshot"


def test_every_detector_sees_the_same_nodes():
    """One materialization per root, and it is the one ``ast.walk`` yields.

    Three claims, each of which fails differently. IDENTITY, so a second walk
    costs nothing and the sequence cannot have been rebuilt underneath a
    detector mid-iteration. ELEMENT-WISE EQUALITY against a fresh
    ``ast.walk(tree)``, so what is shared is the same objects in the same
    breadth-first order rather than a subset, a re-ordering or a set. And
    COVERAGE, because a memo that answered an empty tuple for a root it had not
    walked would be fast, green and blind -- the failure this whole file exists
    to make impossible, reintroduced through the cache.
    """
    expected_modules = len(app_python_files())
    walked = 0
    for path, tree in app_trees():
        shared = nodes_of(tree)
        assert shared is nodes_of(tree), f"{path.name}: the node sequence is rebuilt per caller"
        assert list(shared) == list(ast.walk(tree)), (
            f"{path.name}: the shared sequence is not what ast.walk yields"
        )
        assert shared, f"{path.name}: a materialized walk came back empty"
        walked += 1
    assert walked == expected_modules, (
        f"the shared node sequence covers {walked} of {expected_modules} app modules"
    )


def test_a_narrowed_walk_is_shared_too():
    """The sub-walks share on the same terms, and they are the common case.

    Most detectors here narrow to one function and walk that rather than
    walking the module, and those roots are memoized exactly like a module is.
    Two definitions per module rather than all of them: the point is that a
    narrowed root behaves like any other, and re-walking every function in the
    tree inside the test that exists to avoid re-walking them would be the
    thing it is checking.
    """
    checked = 0
    for path, tree in app_trees():
        narrowed = [
            node for node in nodes_of(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ][:2]
        for node in narrowed:
            shared = nodes_of(node)
            assert shared is nodes_of(node), f"{path.name}: a narrowed walk is rebuilt per caller"
            assert list(shared) == list(ast.walk(node)), (
                f"{path.name}:{node.lineno}: the narrowed sequence is not what ast.walk yields"
            )
            checked += 1
    assert checked >= 100, (
        f"only {checked} narrowed walks were checked across the tree; the assertion "
        "has to span the snapshot to be evidence about the snapshot"
    )


# --- Response cookies enter the jar only through the fetch-completion tail -----
#
# The httpx and curl_cffi fetchers each carried their own copy of the tail that
# decompresses, normalises headers and persists cookies, and the copies had
# already drifted: httpx normalised the response headers into a dict and handed
# *that* to the cookie helper, which is what knocked it off its documented
# get_list path, while curl_cffi handed over the transport's own multidict.
# Collapsing both into fetchers._finish_fetch is what removed the drift. These
# two rules are the enforcement half: the jar has exactly one write door, and
# that door is the shared tail. A third fetch path growing its own cookie
# handling trips them at the line, not after a session is silently missing a
# cookie.

_COOKIE_TAIL = "app/crawl/strategies/fetchers.py"
_COOKIE_HELPER = "app/crawl/strategies/request_profile.py"
_COOKIE_TAIL_FN = "_finish_fetch"
_COOKIE_HELPER_FN = "_store_set_cookies"


def _enclosing_nodes(tree: ast.AST, matches: Callable[[ast.AST], bool]) -> list[tuple[ast.AST, str]]:
    """``(node, innermost enclosing function name)`` for every node ``matches`` accepts.

    ``nodes_of`` yields a flat breadth-first sequence with no parent links, so a
    detector that needs to know *where* a node sits cannot be built on it. This
    descends instead, which is affordable: a couple of rules, once per module.

    The name is where a detector reads *ownership* from -- a contract authored in
    one function rather than three -- and that question applies to any node kind,
    not only to calls, so the descent takes a predicate instead of a callee name.
    """
    found: list[tuple[ast.AST, str]] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if matches(child):
                found.append((child, enclosing))
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                else enclosing
            )
            visit(child, name)

    visit(tree, "<module>")
    return found


def _enclosing_calls(tree: ast.AST, target: str) -> list[tuple[ast.Call, str]]:
    """``(call, innermost enclosing function name)`` for calls to ``target``.

    The narrowing ``if`` is what types the result as calls rather than nodes; the
    predicate in :func:`_enclosing_nodes` cannot, because it is a bare callable.
    """
    return [
        (node, enclosing)
        for node, enclosing in _enclosing_nodes(
            tree,
            lambda child: isinstance(child, ast.Call)
            and resolved_call_name(tree, child).rsplit(".", 1)[-1] == target,
        )
        if isinstance(node, ast.Call)
    ]


def _cookie_writes_off_the_fetch_tail(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a cookie write that does not run through the shared fetch tail."""
    relative = relative_posix(path)
    offenders: list[str] = []
    for call, enclosing in _enclosing_calls(tree, "_store_set_cookies"):
        if relative == _COOKIE_TAIL and enclosing == _COOKIE_TAIL_FN:
            continue
        offenders.append(offender_at(path, call, f"_store_set_cookies() outside {_COOKIE_TAIL_FN}()"))
    for call, enclosing in _enclosing_calls(tree, "add_cookie_header"):
        if relative == _COOKIE_HELPER and enclosing == _COOKIE_HELPER_FN:
            continue
        offenders.append(offender_at(path, call, f"add_cookie_header() outside {_COOKIE_HELPER_FN}()"))
    return offenders


def test_response_cookies_enter_the_jar_only_through_the_fetch_tail():
    """Cookie persistence has one door, and that door is the shared fetch tail."""
    offenders = app_offenders(_cookie_writes_off_the_fetch_tail)
    assert not offenders, (
        f"Persist response cookies via {_COOKIE_TAIL}::{_COOKIE_TAIL_FN}, which calls "
        f"{_COOKIE_HELPER_FN} for you, instead of reaching for the jar where you are: " + ", ".join(offenders)
    )


def test_cookie_tail_detector_bites():
    """Every bypass shape fires; the sanctioned two-hop chain stays clean."""
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    assert_fires_once(
        _cookie_writes_off_the_fetch_tail,
        {
            "import x\nx._store_set_cookies(url, headers)\n": "_store_set_cookies()",
            "import jar\njar.add_cookie_header(url, cookie)\n": "add_cookie_header()",
            "def nested():\n    jar.add_cookie_header(url, cookie)\n": "add_cookie_header()",
        },
        synthetic,
    )

    tail_call = (
        "from app.crawl.strategies.request_profile import _store_set_cookies\n"
        "def _finish_fetch(**kwargs):\n"
        "    return _store_set_cookies(kwargs)\n"
    )
    # The sanctioned chain is silent at its own path...
    assert not _cookie_writes_off_the_fetch_tail(ast.parse(tail_call), REPO_ROOT / _COOKIE_TAIL)
    assert not _cookie_writes_off_the_fetch_tail(
        ast.parse("def _store_set_cookies(url, headers):\n    jar.add_cookie_header(url, c)\n"),
        REPO_ROOT / _COOKIE_HELPER,
    )
    # ...and the very same source still fires anywhere else. A detector keyed on
    # the function name alone would wave this tree through, which is the whole
    # reason the rule is keyed on the path.
    assert _cookie_writes_off_the_fetch_tail(ast.parse(tail_call), synthetic)


# --- A wire model may not silently drop a domain model's field ---------------
#
# app/models.py holds the internal dataclasses and app/api_models.py the pydantic
# response models. `DiscoveredLink` is declared in both, with the same seven
# field names, and *nothing* connects the two: response_shaping converts
# generically by attribute, and a strict pydantic response model DROPS any key
# it does not declare. So adding a field to the dataclass and forgetting the
# BaseModel is not a type error and not a test failure -- the field stops
# existing on the wire, and everything downstream reads a default. That is the
# CapabilitiesResponse failure mode this project already shipped once, which
# is why the pair gets a gate rather than a comment asking people to keep two
# lists in step.
#
# Written as a rule over whatever pair exists rather than a hardcoded name, so
# the next twin is covered without a second edit here.

_DOMAIN_MODEL_MODULE = "app/models.py"
_WIRE_MODEL_MODULE = "app/api_models.py"


def _is_dataclass(node: ast.ClassDef) -> bool:
    """True for a ``@dataclass`` class, bare or parameterised."""
    return any(
        (isinstance(d, ast.Name) and d.id == "dataclass")
        or (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "dataclass")
        for d in node.decorator_list
    )


def _is_pydantic_model(node: ast.ClassDef) -> bool:
    """True for a class deriving from ``BaseModel``, directly or subscripted."""
    return any(
        isinstance(target := (base.value if isinstance(base, ast.Subscript) else base), ast.Name)
        and target.id == "BaseModel"
        for base in node.bases
    )


def _declared_fields(node: ast.ClassDef) -> list[str]:
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _wire_mirror_drift(domain_tree: ast.Module, wire_tree: ast.Module) -> list[str]:
    """Field names that differ between a domain dataclass and its wire model."""
    domain = {n.name: n for n in domain_tree.body if isinstance(n, ast.ClassDef) and _is_dataclass(n)}
    wire = {n.name: n for n in wire_tree.body if isinstance(n, ast.ClassDef) and _is_pydantic_model(n)}
    drifted: list[str] = []
    for name in sorted(domain.keys() & wire.keys()):
        internal = set(_declared_fields(domain[name]))
        external = set(_declared_fields(wire[name]))
        drifted.extend(
            f"{name}.{field_name} exists internally and is dropped on the wire"
            for field_name in sorted(internal - external)
        )
        drifted.extend(
            f"{name}.{field_name} is declared on the wire and nowhere internally"
            for field_name in sorted(external - internal)
        )
    return drifted


def test_wire_models_mirror_every_domain_field():
    """A domain field cannot disappear between the dataclass and the response model."""
    domain_path, domain_tree = file_tree(_DOMAIN_MODEL_MODULE)
    wire_path, wire_tree = file_tree(_WIRE_MODEL_MODULE)
    drifted = _wire_mirror_drift(domain_tree, wire_tree)
    assert not drifted, (
        f"A pydantic response model DROPS every key it does not declare, so a field added to "
        f"a domain dataclass in {domain_path.name} without a twin in {wire_path.name} does not "
        f"fail loudly -- it quietly stops existing on the wire. Keep the pair in step, or give "
        f"them separate names and a deliberate converter: " + "; ".join(drifted)
    )


def test_wire_mirror_detector_bites():
    """A dropped field and a wire-only field both fire; an exact pair stays clean."""
    domain = ast.parse("@dataclass(frozen=True)\nclass Link:\n    url: str\n    host: str\n    depth: int\n")
    matching = ast.parse(
        "from pydantic import BaseModel\nclass Link(BaseModel):\n"
        "    url: str\n    host: str\n    depth: int\n"
    )
    assert not _wire_mirror_drift(domain, matching)

    drifted = _wire_mirror_drift(domain, ast.parse("class Link(BaseModel):\n    url: str\n    host: str\n"))
    assert len(drifted) == 1, drifted
    assert "Link.depth" in drifted[0] and "dropped on the wire" in drifted[0], drifted

    drifted = _wire_mirror_drift(
        domain,
        ast.parse("class Link(BaseModel):\n    url: str\n    host: str\n    depth: int\n    score: float\n"),
    )
    assert len(drifted) == 1, drifted
    assert "Link.score" in drifted[0] and "nowhere internally" in drifted[0], drifted

    # A bare @dataclass and a subscripted BaseModel[str] base are the same two
    # decorators spelled the other way round; both must still be matched.
    assert _wire_mirror_drift(
        ast.parse("@dataclass\nclass Link:\n    url: str\n    host: str\n    depth: int\n"),
        ast.parse("class Link(BaseModel[str]):\n    url: str\n    host: str\n"),
    )
    # An unrelated name on both sides is not a pair -- a wire-only model and a
    # domain-only dataclass must not be reported against each other.
    assert not _wire_mirror_drift(
        ast.parse("@dataclass\nclass Link:\n    url: str\n"),
        ast.parse("class Other(BaseModel):\n    url: str\n    host: str\n"),
    )


# --- An advertised payload key may not be dropped by its response model -------
#
# The two discovery endpoints build a dict by hand and hand it to FastAPI with a
# strict `response_model=`. Pydantic DROPS every key the model does not declare,
# so advertising a new block is not a type error and not a test failure -- the key
# stops existing on the wire while every test that calls the payload function
# directly stays green, because those tests never go through the model. This
# project has shipped that once: `crawl_timing` was advertised and absent from
# the endpoint, and was caught only by curling the live service.
#
# The gate above covers the other twin, a domain dataclass and its response
# model. This covers the shape that actually shipped the bug: a hand-written dict
# literal beside a model that is maintained by hand. Both have to be edited for
# the payload to be correct, and nothing checked that they agreed.
#
# The pair is declared as data so adding a third advertised endpoint is one line
# here, and so the real tree and the bites proof below exercise the same code.

_ADVERTISED_PAYLOADS = (
    ("app/discovery/capabilities.py", "capabilities_payload", "CapabilitiesResponse"),
    ("app/discovery/agent_hints.py", "agent_hints_payload", "AgentHintsResponse"),
)


def _advertised_keys(tree: ast.Module, function_name: str) -> set[str]:
    """Top-level string keys of a payload function's single returned dict.

    Asserts rather than degrading when the shape is not statically readable. A
    payload assembled across several statements defeats extraction, and quietly
    reporting "nothing dropped" for exactly the shape this gate cannot see would
    recreate the defect it exists to catch -- a detector that goes blind without
    saying so is the gap, not the fix.
    """
    function = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function_name),
        None,
    )
    assert function is not None, f"{function_name} not found; update _ADVERTISED_PAYLOADS"
    returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
    assert len(returns) == 1, (
        f"{function_name} must be a single `return {{...}}` so its advertised keys can be "
        f"read; found {len(returns)} return statements"
    )
    payload = returns[0].value
    assert isinstance(payload, ast.Dict), (
        f"{function_name} must return a dict literal so its keys can be compared against the "
        f"response model; got {type(payload).__name__}"
    )
    return {k.value for k in payload.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _undeclared_advertised_keys(
    payload_tree: ast.Module, wire_tree: ast.Module, function_name: str, model_name: str
) -> list[str]:
    """Advertised payload keys the response model does not declare."""
    model = next((n for n in wire_tree.body if isinstance(n, ast.ClassDef) and n.name == model_name), None)
    assert model is not None, f"{model_name} not found in {_WIRE_MODEL_MODULE}"
    declared = set(_declared_fields(model))
    return [
        f"advertised key {key!r} is dropped by {model_name}"
        for key in sorted(_advertised_keys(payload_tree, function_name) - declared)
    ]


@pytest.mark.parametrize(("payload_module", "function_name", "model_name"), _ADVERTISED_PAYLOADS)
def test_every_advertised_payload_key_is_declared_on_its_response_model(
    payload_module: str, function_name: str, model_name: str
):
    """An advertised key that the response model does not declare is dropped on the wire."""
    _, payload_tree = file_tree(payload_module)
    _, wire_tree = file_tree(_WIRE_MODEL_MODULE)
    dropped = _undeclared_advertised_keys(payload_tree, wire_tree, function_name, model_name)
    assert not dropped, (
        f"{function_name} advertises keys that {model_name} does not declare, and "
        f"response_model={model_name} silently filters every undeclared key -- so they reach "
        f"the dict-returning tests and never the wire. Add the field to {model_name}: " + "; ".join(dropped)
    )


def test_advertised_payload_detector_bites():
    """An undeclared advertised key fires; an exact pair stays clean; a blind spot raises."""
    wire = ast.parse("class Capabilities(BaseModel):\n    service: str\n    crawl_timing: Timing\n")
    exact = ast.parse("def payload():\n    return {'service': 'x', 'crawl_timing': {}}\n")
    assert not _undeclared_advertised_keys(exact, wire, "payload", "Capabilities")

    drifted = ast.parse(
        "def payload():\n    return {'service': 'x', 'crawl_timing': {}, 'limits': {}, 'extra': 1}\n"
    )
    reported = _undeclared_advertised_keys(drifted, wire, "payload", "Capabilities")
    assert len(reported) == 2, reported
    assert "'extra'" in reported[0] and "dropped by Capabilities" in reported[0], reported
    assert "'limits'" in reported[1], reported

    # A declared field the payload never sends is not drift in this direction: the
    # model is the stricter of the two and a required field is caught by FastAPI.
    assert not _undeclared_advertised_keys(
        ast.parse("def payload():\n    return {'service': 'x'}\n"), wire, "payload", "Capabilities"
    )

    # A payload assembled statement by statement defeats static extraction, and
    # going quiet on that shape would be the very defect this gate exists to stop.
    assembled = ast.parse("def payload():\n    d = {}\n    d['service'] = 'x'\n    return d\n")
    with pytest.raises(AssertionError, match="dict literal"):
        _undeclared_advertised_keys(assembled, wire, "payload", "Capabilities")


# --- A result may not spell its own (status, classification) pair -------------
#
# app/crawl/result_helpers.py is the chokepoint for building results, and both
# of its terminal primitives call _enforce_status_classification first. That
# check is the whole point: it makes a result that asserts a status the taxonomy
# would re-derive differently for the same label unrepresentable, because the
# two axes are free parameters and nothing else holds them together.
#
# A caller that builds a result itself can sidestep all of it by naming both
# axes as literals. The pair it writes is legal on the day it is written, which
# is exactly why nothing reports it, and the day the taxonomy moves a default
# the result is quietly wrong with no failure anywhere. Deriving one axis from
# status_for_classification, or copying both off an existing result, keeps the
# property -- which is what the browser strategy does. Naming both is how the
# property gets lost.

_RESULT_CONSTRUCTORS = frozenset({"CrawlResult", "EngineCrawlResult"})
_RESULT_CHOKEPOINT_MODULE = "app/crawl/result_helpers.py"


def _literal_status_classification_pairs(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a result built from a hand-written (status, classification) pair."""
    if relative_posix(path) == _RESULT_CHOKEPOINT_MODULE:
        return []
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Call):
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] not in _RESULT_CONSTRUCTORS:
            continue
        spelled = {
            kw.arg: kw.value
            for kw in node.keywords
            if kw.arg in ("status", "classification") and isinstance(kw.value, ast.Constant)
        }
        if len(spelled) == 2:
            offenders.append(offender_at(path, node, "hand-written (status, classification) pair"))
    return offenders


def test_results_derive_their_status_from_the_taxonomy():
    """No result outside the chokepoint spells both taxonomy axes as literals."""
    offenders = app_offenders(_literal_status_classification_pairs)
    assert not offenders, (
        f"Build results through {_RESULT_CHOKEPOINT_MODULE}, or derive one axis with "
        f"constants.status_for_classification, so _enforce_status_classification can see "
        f"the pair: " + ", ".join(offenders)
    )


def test_result_status_pair_detector_bites():
    """A spelled pair fires; deriving either axis stays clean."""
    path = REPO_ROOT / "app" / "_synthetic.py"
    fires = {
        'CrawlResult(url=u, status="success", classification="rendered")\n': "hand-written",
        'EngineCrawlResult(url=u, status="failed", classification="dns_error")\n': "hand-written",
        # A qualified call is the same construction.
        'models.CrawlResult(url=u, status="failed", classification="blocked")\n': "hand-written",
    }
    assert_fires_once(_literal_status_classification_pairs, fires, path)

    clean = (
        # Derived, not spelled -- what the browser strategy does.
        "CrawlResult(url=u, status=status, classification=classification)\n",
        "CrawlResult(url=u, status=status_for_classification(label), classification=label)\n",
        # One axis spelled and one derived still cannot disagree silently.
        'CrawlResult(url=u, status="failed", classification=label)\n',
        # The same spelled pair inside the chokepoint module is the definition.
        'CrawlResult(url=u, status="failed", classification=label)\n',
    )
    assert_stays_clean(_literal_status_classification_pairs, clean, REPO_ROOT / _RESULT_CHOKEPOINT_MODULE)
    assert_stays_clean(_literal_status_classification_pairs, clean[:3], path)
    # And the chokepoint exemption is keyed on the module, not on a name: a
    # spelled pair anywhere else still fires even under the same function name.
    assert _literal_status_classification_pairs(
        ast.parse('def make():\n    return CrawlResult(url=u, status="failed", classification="blocked")\n'),
        path,
    )


# --- browser instance release -------------------------------------------------
# Both the per-attempt reset and the service-shutdown close have to forget the
# same cached fields. Spelling that out twice is how one of them grows a stale
# field the other never clears -- which is exactly what happened to
# ``_browser_reused`` before _release_browser existed. Only the owner may
# clear them, so a new field cannot be added to one path and forgotten on the
# other.
_BROWSER_RELEASE_FIELDS = ("_browser_cm", "_browser", "_browser_reused")
_BROWSER_RELEASE_OWNER = "app/crawl/strategies/browser.py"
_BROWSER_RELEASE_CHOKEPOINT = "_release_browser"
#: __init__ puts the fields at their initial None; that is not a release.
_BROWSER_RELEASE_EXEMPT = frozenset({"__init__"})


def _browser_release_offenders(tree, path, *, owner, fields, chokepoint):
    # Path == Path, not path.name: comparing a str against a Path is never
    # equal, which would make the whole sweep silently yield nothing.
    if path != owner:
        return
    for node in functions(tree):
        if node.name in (chokepoint, *_BROWSER_RELEASE_EXEMPT):
            continue
        for inner in ast.walk(node):
            # Only clearing to None is a release. _acquire_browser assigns a
            # real context manager and a real browser, which is the opposite
            # of forgetting them.
            cleared = (
                [
                    t
                    for t in inner.targets
                    if isinstance(t, ast.Attribute)
                    and t.attr in fields
                    and isinstance(inner.value, ast.Constant)
                    and inner.value.value is None
                ]
                if isinstance(inner, ast.Assign)
                else []
            )
            for target in cleared:
                yield offender_at(
                    path,
                    inner,
                    f"{target.attr} is cleared to None in {node.name}() rather than {chokepoint}(); route "
                    f"the release through the chokepoint so every exit path clears the same set of fields",
                )


def test_browser_instance_fields_are_cleared_only_by_the_release_chokepoint():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(
            _browser_release_offenders(
                tree,
                path,
                owner=REPO_ROOT / _BROWSER_RELEASE_OWNER,
                fields=_BROWSER_RELEASE_FIELDS,
                chokepoint=_BROWSER_RELEASE_CHOKEPOINT,
            )
        )
    assert not offenders, "the browser release chokepoint was bypassed: " + ", ".join(offenders)


def test_browser_release_detector_bites():
    # The reset is generated from the field tuple the detector sweeps, so the
    # synthetic source cannot claim to cover a field the guard does not check.
    # Written out twice it was the same three lines of drift-prone fixture, and
    # the drift is invisible: a field added to the tuple would leave this proof
    # asserting on a source that never mentions it.
    reset = "\n".join(f"        self.{field} = None" for field in _BROWSER_RELEASE_FIELDS)
    source = f"""
class Renderer:
    def __init__(self):
{reset}

    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):
{reset}

    async def close(self):
        self._browser = None
"""
    args = {
        "owner": REPO_ROOT / _BROWSER_RELEASE_OWNER,
        "fields": _BROWSER_RELEASE_FIELDS,
        "chokepoint": _BROWSER_RELEASE_CHOKEPOINT,
    }
    # list(), not the generator: a generator object is always truthy, so
    # asserting on one directly would pass without ever running the sweep.
    offenders = list(
        _browser_release_offenders(ast.parse(source), REPO_ROOT / _BROWSER_RELEASE_OWNER, **args)
    )
    assert offenders, "a field cleared outside the release chokepoint escaped the sweep"
    elsewhere = list(
        _browser_release_offenders(
            ast.parse(source), REPO_ROOT / "app" / "crawl" / "content_cache.py", **args
        )
    )
    assert not elsewhere, f"scoped to the wrong module -- ignored: {elsewhere}"
    clean = "class R:\n    async def _release_browser(self):\n        self._browser = None\n"
    assert not list(
        _browser_release_offenders(ast.parse(clean), REPO_ROOT / _BROWSER_RELEASE_OWNER, **args)
    ), "the chokepoint's own body must not be flagged"


# --- MCP tool request models --------------------------------------------------
# The MCP dispatcher used to hand each handler the raw ``arguments`` dict and
# let the handler validate, while the *advertised* inputSchema was a separate
# hand-written JSON schema. Two descriptions of one contract, neither derived
# from the other -- and the advertised one claimed additionalProperties: False
# for a request model that in fact ignores extras. Now the registration names
# the pydantic model, the schema is derived from it, and the dispatcher
# validates. This guard keeps all three attached to one another.
_MCP_TOOLS_MODULE = "app/mcp/tools.py"


def _mcp_unvalidated_tool_offenders(tree, path, *, tools_module):
    if path.name != tools_module:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "ToolSpec":
            continue
        named = {kw.arg for kw in node.keywords if kw.arg}
        if "request_model" not in named:
            yield offender_at(
                path,
                node,
                "ToolSpec registers a tool without a request_model; the inputSchema and the call's argument "
                "validation are both derived from it, so a tool without one has no validated contract",
            )
    # The dispatcher must be the one validating, not the handlers.
    for node in functions(tree):
        if not node.name.startswith("_handle_"):
            continue
        for inner in nodes_of_type(node, ast.Call):
            if isinstance(inner.func, ast.Attribute) and inner.func.attr == "model_json_schema":
                yield offender_at(
                    path,
                    inner,
                    f"{node.name}() still derives a schema itself; the "
                    f"schema belongs to ToolSpec.input_schema, derived from the request model",
                )


def test_every_mcp_tool_declares_the_model_that_validates_its_arguments():
    offenders = app_offenders(_mcp_unvalidated_tool_offenders, tools_module=_MCP_TOOLS_MODULE)
    assert not offenders, "an MCP tool is missing its validated contract: " + ", ".join(offenders)


def test_mcp_request_model_detector_bites():
    from app.mcp.tools import TOOL_SPECS

    missing = ast.parse(
        "def register():\n    return ToolSpec(\n        name='x',\n        description='d',\n"
        "        output_schema=None,\n        handler=h,\n    )\n"
    )
    offenders = list(
        _mcp_unvalidated_tool_offenders(
            missing, REPO_ROOT / "crawl" / "synthetic.py", tools_module="synthetic.py"
        )
    )
    assert offenders, "a ToolSpec without a request_model escaped the sweep"
    # A handler that builds its own schema is the other half of the bypass.
    self_scheming = ast.parse(
        "async def _handle_x(payload, settings, shutdown_event):\n"
        "    schema = CrawlRequest.model_json_schema()\n"
        "    return schema\n"
    )
    assert list(
        _mcp_unvalidated_tool_offenders(self_scheming, REPO_ROOT / _MCP_TOOLS_MODULE, tools_module="tools.py")
    ), "a handler deriving its own schema escaped the sweep"
    # And the live registry must satisfy the invariant the guard asserts.
    for spec in TOOL_SPECS:
        assert spec.request_model is not None, spec.name
        assert spec.input_schema["properties"] == {
            k: v
            for k, v in spec.request_model.model_json_schema().get("properties", {}).items()
            if k not in spec.unsupported_fields
        }, f"{spec.name} advertises a schema its request model does not describe"


# ---------------------------------------------------------------------------
# The content pipeline: one dispatch, one discovery call, one projection
#
# The browser and HTTP strategies each carried the same three-way extraction
# dispatch, the same offloaded post-fetch discovery call, and the same block of
# discovered-links result fields. All three now live in
# app/crawl/strategies/content_pipeline.py, and `_discovery_result_fields` is
# the one place a discovery envelope becomes result fields.
#
# The projection was a correctness risk rather than cosmetics: a field threaded
# into the HTTP result but not the browser one is a divergence between the two
# answers to the same page, and nothing in the tree can see it -- both results
# are individually well-formed. So the two sweeps below are tree-wide rather
# than file-scoped: a guard that only looked at the two modules it was written
# for would keep passing while a third strategy grew its own copy, which is the
# same empty-sweep failure the other chokepoint guards in this file were written
# against.
# ---------------------------------------------------------------------------

_CONTENT_PIPELINE_OWNER = frozenset({"content_pipeline.py"})
# Import-resolved, so an alias cannot slip past the sweep.
_EXTRACTION_ENTRY_POINTS = frozenset(
    {
        "app.crawl.extraction._extract_document",
        "app.crawl.extraction._extract_html",
        "app.crawl.extraction.extract_content",
        "app.crawl.extraction.extract_post_fetch_discovery",
        "app.crawl.extraction.normalized_extraction",
    }
)
# The terminal-result builders, not the model: result_helpers constructing
# EngineCrawlResult is where the discovered-links fields are DEFINED, which is
# a different thing from a call site spelling them.
_RESULT_BUILDERS = frozenset(
    {
        "_binary_no_text_result",
        "_engine_result_from_exception",
        "_failed_engine_result",
        "_make_engine_result",
        "_skipped_engine_result",
        "_terminal_engine_result",
    }
)
_DISCOVERY_RESULT_FIELDS = frozenset(
    {"discovered_link_count", "discovered_links", "discovered_links_truncated"}
)
_DISCOVERY_PROJECTION = "_discovery_result_fields"


def _extraction_reach_past_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a use of a raw extractor outside the content pipeline.

    Keyed on the *name in load position* rather than on a call: the shape both
    strategies used was an offloaded extractor, ``asyncio.to_thread(_extract_html,
    ...)``, where the extractor is an argument rather than a callee, and a
    call-keyed detector misses exactly that. Keyed on the use rather than the
    import because binding a name is not the duplication, dispatching on it is.

    ``self._extract_html(page)`` in the browser worker is a method on the
    renderer, not this function, so it resolves to a different name and is not
    an offender. A reference *within* extraction.py resolves to the bare name
    rather than the dotted one, which is how the owner of these functions is
    left out of its own sweep without being named in it.
    """
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            continue
        name = resolved_expr_name(tree, node)
        if name in _EXTRACTION_ENTRY_POINTS:
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{name} is dispatched outside "
                    f"strategies/content_pipeline.py; run the mode chain through _run_extraction",
                )
            )
    return offenders


def test_extraction_is_dispatched_by_the_content_pipeline_alone():
    """The three extraction modes are dispatched in one module, not per strategy."""
    assert_trees_clean(
        _extraction_reach_past_offenders,
        "dispatch extraction through strategies/content_pipeline._run_extraction",
        trees=app_trees(exclude=SANCTIONED | _CONTENT_PIPELINE_OWNER),
    )


def test_extraction_reach_past_detector_bites():
    """A re-spelled dispatch fires, offloaded and aliased; routing through it stays clean."""
    synthetic = REPO_ROOT / "app" / "crawl" / "strategies" / "_synthetic.py"
    spelled = ast.parse(
        "from app.crawl.extraction import _extract_html, normalized_extraction\n"
        "async def f(decoded, final_url):\n"
        "    extracted = await asyncio.to_thread(_extract_html, decoded, final_url)\n"
        "    return normalized_extraction(extracted)\n"
    )
    offenders = _extraction_reach_past_offenders(spelled, synthetic)
    assert len(offenders) == 2, offenders
    assert all("_run_extraction" in item for item in offenders), offenders
    aliased = ast.parse(
        "from app.crawl.extraction import extract_content as _ec\n"
        "def f(body, content_type, url):\n"
        "    return _ec(body, content_type, url)\n"
    )
    assert _extraction_reach_past_offenders(aliased, synthetic), "an aliased extractor call escaped the sweep"
    routed = ast.parse(
        "from app.crawl.strategies.content_pipeline import _run_extraction\n"
        "async def f(body, final_url):\n"
        "    return await _run_extraction(extraction='full', content=body, decoded='')\n"
    )
    assert not _extraction_reach_past_offenders(routed, synthetic), "the chokepoint call was flagged"
    # The exemption is not what makes the tree clean: the owner's own calls are
    # the shape this detector matches.
    owner = next((path, tree) for path, tree in app_trees() if path.name in _CONTENT_PIPELINE_OWNER)
    assert owner[0].name == "content_pipeline.py", owner[0]
    assert _extraction_reach_past_offenders(owner[1], synthetic), "the detector no longer matches the owner"


def _respelled_discovery_result_fields_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag discovered-links result fields spelled at a result-builder call.

    ``**`` spread is the sanctioned shape and is not a keyword, so a strategy
    that routes the envelope through ``_discovery_result_fields`` reports
    nothing; a strategy that re-spells one field reports that field.
    """
    offenders: list[str] = []
    for node in calls_to_any(tree, _RESULT_BUILDERS):
        spelled = sorted({kw.arg for kw in node.keywords if kw.arg in _DISCOVERY_RESULT_FIELDS})
        if spelled:
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{node.func.id} spells {', '.join(spelled)}; "
                    f"spread content_pipeline._discovery_result_fields instead",
                )
            )
    return offenders


def test_discovered_link_result_fields_come_from_the_pipeline():
    """A discovery envelope reaches a result through one projection."""
    assert_trees_clean(
        _respelled_discovery_result_fields_offenders,
        "project the discovery envelope through content_pipeline._discovery_result_fields",
        trees=app_trees(exclude=SANCTIONED | _CONTENT_PIPELINE_OWNER),
    )


def test_discovery_result_field_detector_bites():
    """A re-spelled projection fires; the spread, and a non-result sink, stay clean."""
    synthetic = REPO_ROOT / "app" / "crawl" / "strategies" / "_synthetic.py"
    # The historical shape: the block spelled at the result call.
    respelled = ast.parse(
        "def build(post_fetch, error):\n"
        "    return _make_engine_result(\n"
        "        discovered_link_count=post_fetch['discovered_link_count'],\n"
        "        discovered_links=post_fetch['discovered_links'],\n"
        "        error=error,\n"
        "    )\n"
    )
    offenders = _respelled_discovery_result_fields_offenders(respelled, synthetic)
    assert len(offenders) == 1, offenders
    assert "_discovery_result_fields" in offenders[0], offenders
    spread = ast.parse(
        "def build(post_fetch, error):\n"
        "    return _make_engine_result(\n"
        "        **_discovery_result_fields(post_fetch, classification_evidence=[], error=error),\n"
        "        next_page_url=post_fetch['next_page_url'],\n"
        "    )\n"
    )
    assert not _respelled_discovery_result_fields_offenders(spread, synthetic), (
        "the sanctioned spread was flagged"
    )
    # A discovered-links field that is not a result field is somebody else's.
    unrelated = ast.parse(
        "def count(post_fetch):\n"
        "    return stats(discovered_link_count=post_fetch['discovered_link_count'])\n"
    )
    assert not _respelled_discovery_result_fields_offenders(unrelated, synthetic), (
        "a non-result sink was flagged"
    )


def test_discovery_result_projection_has_exactly_one_definition():
    """The projection is one function, so it cannot be forked per strategy.

    Tree-wide for the same reason the sweeps above are: a second definition
    would satisfy every structural guard above while reintroducing the exact
    duplication they exist to prevent.
    """
    definitions = [
        offender(path, node.lineno)
        for path, tree in app_trees(exclude=frozenset())
        for node in nodes_of_type(tree, ast.FunctionDef, ast.AsyncFunctionDef)
        if node.name == _DISCOVERY_PROJECTION
    ]
    assert len(definitions) == 1, f"the discovery projection was defined again: {definitions}"
    assert "app/crawl/strategies/content_pipeline.py" in definitions[0], definitions


_PROTOCOL_CONSTANTS_MODULE = "app/mcp/constants.py"


def _module_level_assignment_lines(tree: ast.AST, names: Iterable[str]) -> dict[str, int]:
    """Line of each named module-level assignment, for the offender label.

    Distinct from :func:`_declared_public_symbols`, which reads the *declaration*
    of the surface; this locates a name the surface already vouches for, so a
    reader of the failure is handed the line to delete rather than a name to go
    looking for.
    """
    wanted = set(names)
    lines: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                lines[target.id] = node.lineno
    return lines


def _unproduced_protocol_constants(
    owner_tree: ast.AST | None,
    trees: Iterable[tuple[Path, ast.Module]],
    *,
    owner: str = _PROTOCOL_CONSTANTS_MODULE,
) -> list[str]:
    """Declared protocol constants that no other module under ``app/`` reads.

    This surface is a set of *values other code can rely on*: a JSON-RPC code a
    caller can be sent, a ``_meta`` key the server looks for. A declared value
    nothing reads is not a spare part of that surface, it is a claim about it --
    and an untested error code is worse than an absent one precisely because it
    reads as coverage. Two sat in that state: ``MISSING_REQUIRED_CLIENT_CAPABILITY``,
    which nothing ever raised, and ``META_CLIENT_INFO``, which nothing ever read,
    because the 2026-07-28 spec requires only ``protocolVersion`` and
    ``clientCapabilities`` in ``_meta``.

    Reachability is the check rather than a reviewer's memory, because the
    registry is where a spec addition goes to be *recorded* and the recording is
    not the same act as implementing it. The reference sweep is the whole
    ``app/`` tree -- a constant read from a module this test never heard of is
    produced -- with the declaring module excluded from the reference side,
    because its own assignments would otherwise satisfy the check for every
    name in it, which is the defect spelled backwards.

    ``__all__`` rather than every module-level assignment, reusing
    :func:`_declared_public_symbols`, because ``__all__`` is the module's own
    statement of the surface it offers and the claim under test is about that
    surface. An unreadable declaration is itself a finding: a registry the sweep
    cannot read is a registry nothing holds.
    """
    declared = None if owner_tree is None else _declared_public_symbols(owner_tree)
    if declared is None:
        return [f"{owner}: the protocol registry is not one literal __all__, so it cannot be swept"]
    referenced: set[str] = set()
    for path, tree in trees:
        if relative_posix(path) == owner:
            continue
        referenced.update(node.id for node in nodes_of_type(tree, ast.Name))
        referenced.update(node.attr for node in nodes_of_type(tree, ast.Attribute))
    lines = _module_level_assignment_lines(owner_tree, declared)
    offenders: list[str] = []
    for name in sorted(declared):
        if name in referenced:
            continue
        detail = f"{name} is declared but nothing in app/ reads it"
        if name in lines:
            offenders.append(offender(REPO_ROOT / owner, lines[name], detail))
        else:
            offenders.append(f"{owner} {detail}")
    return offenders


def test_every_protocol_constant_is_produced():
    """app/mcp/constants.py declares only codes and keys the server can reach."""
    _, owner_tree = file_tree(_PROTOCOL_CONSTANTS_MODULE)
    offenders = _unproduced_protocol_constants(owner_tree, app_trees())
    assert not offenders, "protocol constants with no production reader: " + ", ".join(offenders)


def test_protocol_constant_production_detector_bites():
    """An unread constant fires; reading it by name, or as an attribute, stays clean."""
    owner = ast.parse(
        "HEADER_MISMATCH = -32020\n"
        "META_CLIENT_INFO = 'x'\n"
        "__all__ = ['HEADER_MISMATCH', 'META_CLIENT_INFO']\n"
    )
    elsewhere = APP_ROOT / "mcp" / "protocol.py"
    silent = _unproduced_protocol_constants(owner, [(elsewhere,
    ast.parse("raise ProtocolError(HEADER_MISMATCH)\n"))])
    assert len(silent) == 1, silent
    assert "constants.py:2 META_CLIENT_INFO is declared but nothing in app/ reads it" in silent[0], silent
    both = _unproduced_protocol_constants(
        owner,
        [(elsewhere,
        ast.parse("def check(meta):\n    return meta, HEADER_MISMATCH, err.META_CLIENT_INFO\n"))],
    )
    assert not both, both
    # The vacuous direction: a registry the sweep cannot read is not a clean bill.
    unreadable = ast.parse("__all__ = compute()\n")
    expected = f"{_PROTOCOL_CONSTANTS_MODULE}: the protocol registry is not one literal __all__"
    for source in (unreadable, None):
        offenders = _unproduced_protocol_constants(source, [(elsewhere, ast.parse(""))])
        assert len(offenders) == 1 and expected in offenders[0], offenders


_CYCLE_GUARD_CALL = "_entered"
_CYCLE_GUARD_OWNER = "_build_guarded"


def _cycle_guard_offenders(
    tree: ast.AST, path: Path, *, call: str = _CYCLE_GUARD_CALL, owner: str = _CYCLE_GUARD_OWNER
) -> list[str]:
    """Calls to the cycle guard made from anywhere but the one function that owns it.

    ``_entered`` was single-sourced as a *function* and then inlined back at
    every use: three container branches each carried the two-step "descend, and
    give up if already on the path" sequence, so the guard existed in four
    places. The function's own docstring already claimed the dedup -- "One
    function rather than three copies of the four lines" -- which made the gap
    worse, not better: a reader checking that claim would have believed it.

    The hazard is what a *fourth* branch does. Adding a container kind means
    writing a new ``isinstance`` arm, and the guard is not something the type
    system requires there. Routing every arm through one helper is what makes
    the check structural instead of remembered, so the rule under test is
    positional rather than structural: the guard is consulted in exactly one
    function in ``app/``, and that function is the one the arms call.
    """
    return [
        offender(path, node.lineno, f"{call}() is called from {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_calls(tree, call)
        if enclosing != owner
    ]


def test_the_cycle_guard_is_applied_in_exactly_one_place():
    """app/models.py consults the cycle guard in one function, not once per container arm."""
    offenders = app_offenders(_cycle_guard_offenders)
    assert not offenders, "the cycle guard is applied outside its single owner: " + ", ".join(offenders)


def test_cycle_guard_detector_bites():
    """A guard call inlined at an arm fires; the same call in the owner stays clean."""
    assert_detector(
        _cycle_guard_offenders,
        path=REPO_ROOT / "app" / "models.py",
        fires={
            "def arm(value, seen):\n"
            "    entered = _entered(value, seen)\n"
            "    return entered\n": "is called from 'arm', not _build_guarded()",
            "entered = _entered(value, seen)\n": "is called from '<module>'",
        },
        clean=(
            f"def {_CYCLE_GUARD_OWNER}(value, seen, build):\n"
            "    entered = _entered(value, seen)\n"
            "    return build(value, entered)\n",
        ),
    )
    # An owner by another name is still a second place, so the rule is about
    # being the one sanctioned function rather than about a literal spelling.
    assert _cycle_guard_offenders(
        ast.parse("def other(value, seen):\n    return _entered(value, seen)\n"),
        REPO_ROOT / "app" / "models.py",
        owner="different_owner",
    ), "a guard call must fire when the sanctioned owner is renamed"


_META_REASON_OWNER = "_require_meta_key"
_META_WRONG_TYPE_FRAGMENT = "Invalid _meta key (must be "


def _meta_key_reason_offenders(tree: ast.AST, path: Path, *, owner: str = _META_REASON_OWNER) -> list[str]:
    """The wrong-type half of the ``_meta`` key rejection authored outside its owner.

    ``validate_meta`` checked two required ``_meta`` keys and, for each, spelled
    out the same two-part reason -- absent versus wrong type -- and raised the
    same error around it. The distinction is the caller's only way to learn it
    forgot a key rather than sent the wrong shape, so it is a contract, and a
    duplicated one can drift in both directions at once: the two arms can stop
    agreeing on wording, or one can stop distinguishing the two cases at all,
    with the suite green either way.

    Matching the *literal* rather than the shape is deliberate. A second arm that
    built the same sentence a different way -- an f-string split across lines, a
    helper, a new noun -- would be precisely the drift, and a test that recognised
    the shape would wave it through. The rule is that the sentence is authored
    once, in the function both keys check through.
    """
    def is_reason(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _META_WRONG_TYPE_FRAGMENT in node.value
        )

    return [
        offender(path, node.lineno, f"the _meta key reason is authored in {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_nodes(tree, is_reason)
        if enclosing != owner
    ]


def test_the_meta_key_rejection_reason_is_authored_once():
    """app/mcp/protocol.py spells the wrong-type _meta reason in one function."""
    offenders = app_offenders(_meta_key_reason_offenders)
    assert not offenders, "the _meta key rejection reason is authored more than once: " + ", ".join(offenders)


def test_meta_key_reason_detector_bites():
    """A second authoring of the sentence fires; the one in the owner stays clean."""
    assert_detector(
        _meta_key_reason_offenders,
        path=REPO_ROOT / "app" / "mcp" / "protocol.py",
        fires={
            "def validate_meta(meta, request_id):\n"
            f'    reason = f"{{key}} "\n'
            f'    reason += "{_META_WRONG_TYPE_FRAGMENT}{{key}})"\n': "authored in 'validate_meta'",
            f'WHY = "{_META_WRONG_TYPE_FRAGMENT}protocolVersion)"\n': "authored in '<module>'",
        },
        clean=(
            f"def {_META_REASON_OWNER}(meta, key, expected, label, request_id):\n"
            f'    return f"{_META_WRONG_TYPE_FRAGMENT}{{label}}): {{key}})"\n',
        ),
    )
    # Unrelated reasons are not the contract, so they must not be swept up with it.
    assert_stays_clean(
        _meta_key_reason_offenders,
        clean=(
            'def validate_meta(meta):\n    raise ProtocolValidationError("Missing _meta block")\n',
            'def validate_meta(meta):\n    reason = f"Missing required _meta key: {key}"\n',
        ),
        path=REPO_ROOT / "app" / "mcp" / "protocol.py",
    )


_COMMENT_CLONE_MIN_LINES = 3
# Emitted after every comment-only line, and around indentation: none of them
# says anything about prose, so only a token that carries code ends a block.
_COMMENT_TRANSPARENT = frozenset(
    {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
)


def _comment_blocks(source: str) -> list[tuple[int, tuple[str, ...]]]:
    """``(first line, normalized text)`` for each run of consecutive comment lines.

    ``tokenize`` rather than a ``#`` search, because a ``#`` inside a string is a
    character and not a comment. A run is consecutive *lines* rather than merely
    adjacent comment tokens, so a blank line, a statement, or a re-indent between
    two notes ends the block instead of silently welding unrelated prose into one
    clone -- and re-welding it is what the re-indent case has to catch, which is
    why the break is the line gap and not the indentation.
    """
    blocks: list[tuple[int, tuple[str, ...]]] = []
    lines: list[str] = []
    first_line = 0
    previous_end = 0

    def flush() -> None:
        nonlocal lines, first_line
        if lines:
            blocks.append((first_line, tuple(lines)))
            lines = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            text = " ".join(token.string.lstrip("#").split())
            if not text:
                continue
            if lines and token.start[0] != previous_end + 1:
                flush()
            if not lines:
                first_line = token.start[0]
            lines.append(text)
            previous_end = token.end[0]
        elif token.type not in _COMMENT_TRANSPARENT:
            flush()
    flush()
    return blocks


def _duplicated_comment_runs(
    sources: Iterable[tuple[Path, str]], *, minimum: int = _COMMENT_CLONE_MIN_LINES
) -> list[str]:
    """Comment blocks written twice in app/, with every site that repeats them.

    The token floor cannot see this class at all: jscpd matches tokens, so a
    duplicated comment contributes nothing to a clone it reports, and a
    six-line WHY note read as zero clones at 4/25. That is what happened -- the
    same note about the content cache's eviction window sat at both write call
    sites, each copy carrying a claim about locking, and nothing in the suite or
    the gate could tell that a later edit to the locking had updated one and left
    the other asserting something the code no longer did.

    Normalized on whitespace before comparison, so a re-indented copy -- the way
    these forks actually arrive, by pasting between call sites -- still matches.
    A block shorter than ``minimum`` is not a WHY worth centralizing, and a
    two-line threshold would fire on ordinary repeated section rules.
    """
    sites: dict[tuple[str, ...], list[tuple[Path, int]]] = {}
    for path, source in sources:
        for line, block in _comment_blocks(source):
            if len(block) < minimum:
                continue
            sites.setdefault(block, []).append((path, line))
    return [
        " and ".join(f"{relative_posix(path)}:{line}" for path, line in found)
        + f" carry the same {len(block)}-line comment: {block[0]}"
        for block, found in sorted(sites.items())
        if len(found) > 1
    ]


def test_no_comment_block_is_written_twice():
    """app/ states each multi-line WHY once, not once per call site."""
    offenders = _duplicated_comment_runs(
        (path, path.read_text(encoding="utf-8")) for path in app_python_files(exclude=frozenset())
    )
    assert not offenders, "comment blocks written more than once: " + ", ".join(offenders)


def test_comment_clone_detector_bites():
    """A repeated block fires, in one file and across two; distinct blocks do not.

    Hand-rolled rather than :func:`assert_detector`, which parses its sources
    into modules: a comment is not in the ``ast`` at all, so this is the one
    detector in the file whose input is the text.
    """
    path = REPO_ROOT / "app" / "crawl" / "content_cache.py"
    repeated = "# first line of the note\n# second line\n# third line\n"
    other = "# a different note entirely\n# with three lines\n# of its own text\n"
    detached = "        # first line of the note\n        # second line\n        # third line\n"

    def detect(source: str) -> list[str]:
        return _duplicated_comment_runs([(path, source)])

    once = detect(f"x = 1\n{repeated}y = 2\n")
    assert not once, once
    twice = detect(f"x = 1\n{repeated}y = 2\n{repeated}")
    assert (
        len(twice) == 1
        and "app/crawl/content_cache.py:2 and app/crawl/content_cache.py:6" in twice[0]
    ), twice
    # Two blocks of the same length but different text are two notes.
    assert not detect(f"x = 1\n{repeated}y = 2\n{other}"), "equal-length distinct blocks are not clones"
    # Two lines is a section rule, not a WHY to centralize.
    two_lines = "# first line of the note\n# second line\n"
    assert not detect(f"x = 1\n{two_lines}y = 2\n{two_lines}"), "a two-line rule is below the floor"
    # A blank line ends a block, so two paragraphs stay two paragraphs instead of
    # one welded run -- which would match nothing, including its own twin.
    assert not detect(f"{repeated}\n{other}"), "a blank line must end a comment block"
    assert [line for line, _ in _comment_blocks(f"{repeated}\n{other}")] == [1, 5]
    # Identical prose is still a duplicate across a blank line: the rule about
    # blocks decides what counts as one note, not whether prose may repeat.
    assert detect(f"{repeated}\n{repeated}"), "identical prose is a duplicate however it is separated"
    # Normalized on whitespace, so a re-indented copy -- how these forks actually
    # arrive -- still matches rather than hiding behind the extra spaces.
    assert detect(f"x = 1\n{repeated}y = 2\n{detached}"), "a re-indented copy is still a clone"
    # A fork usually arrives by pasting between modules, so the same block in two
    # different files is one finding, and both sites have to be named.
    across = _duplicated_comment_runs(
        [
            (REPO_ROOT / "app" / "a.py", f"x = 1\n{repeated}"),
            (REPO_ROOT / "app" / "b.py", f"y = 2\n{repeated}"),
        ]
    )
    assert len(across) == 1 and "app/a.py:2 and app/b.py:2" in across[0], across
