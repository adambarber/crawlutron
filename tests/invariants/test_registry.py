"""The property registry, and the checks the registry exists to support.

A registered property is a claim that a code shape is observed everywhere.
These checks keep the registry non-empty, its owners real, and its detectors
honest, plus the meta-invariants about this test suite itself."""

from __future__ import annotations

import ast
import pathlib
from contextvars import ContextVar
from pathlib import Path

from tests.chokepoint_walks import (
    REPO_ROOT,
    TESTS_DIR,
    app_offenders,
    app_trees,
    assert_detector,
    assert_fires_once,
    assert_stays_clean,
    call_nodes,
    has_method_call,
    local_call_leaf_names,
    local_nodes,
    nodes_of,
    offender,
    offender_at,
    relative_posix,
    resolved_call_name,
    trees_under,
)
from tests.invariants._shared import (
    _declared_public_symbols,
    _enclosing_nodes,
    _module_definition_index,
    _relative_app_path,
    assert_property_detector_bites,
    assert_trees_clean,
)
from tests.property_registry import PROPERTY_BY_NAME, PROPERTY_REGISTRY, Property

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
    definition_site="tests/invariants/test_registry.py:_public_surface_offenders",
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
            for node in call_nodes(tree)
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
