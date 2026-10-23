"""Tests that do not run, and doubles that hide the failure they were given.

A ``test_*`` def pytest cannot collect never runs; a double that swallows a
failed result keeps the suite green. Both lie to the reader of a green run."""

from __future__ import annotations

import ast
import configparser
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    TESTS_DIR,
    app_offenders,
    nodes_of,
    offender_at,
    trees_under,
)
from tests.invariants._shared import assert_trees_clean
from tests.membership_gate import (
    aliased_dynamic_pattern_re_offenders,
    aliased_sanctioned_func_offenders,
    error_field_taxonomy_offenders,
    swallowing_test_double_offenders,
)
from tests.rules import Bites, Rule

# --------------------------------------------------------------------------- Collectible tests: a test pytest
# cannot see is a test that never runs


def _collection_prefix() -> str:
    """The name prefix pytest actually collects, read from pytest.ini.

    The rule used to accept any ``test``-spelled name; reading the configured
    ``python_functions`` pattern keeps rule and collector from disagreeing."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    pattern = parser["pytest"]["python_functions"].strip()
    return pattern[: pattern.index("*")]


def _uncollectible_tests(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Report every ``test``-spelled def pytest would silently not collect."""
    prefix = _collection_prefix()
    legal_owners: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(prefix):
            legal_owners.add(id(node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            legal_owners.update(
                id(m)
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith(prefix)
            )

    offenders: list[str] = []
    for test_def in ast.walk(tree):
        if not isinstance(test_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not test_def.name.startswith("test"):
            continue
        if id(test_def) in legal_owners:
            continue
        # A different name from the module-level scan above: the two loops are unrelated passes over the same
        # tree, and reusing the binding made the second one look at the first loop's node.
        owners = [
            parent.name
            for parent in ast.walk(tree)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            and test_def in ast.walk(parent)
            and parent is not test_def
        ]
        where = (
            f"nested inside {owners[0]}()"
            if owners
            else "not a module-level test or a Test* class method named to the collected pattern"
        )
        offenders.append(offender_at(path, test_def, f"test_{test_def.name} is uncollectible -- {where}"))
    return offenders


EVERY_TEST_FUNCTION_IS_ONE_PYTEST_CAN_COLLECT = Rule(
    name="every-test-function-is-one-pytest-can-collect",
    find=_uncollectible_tests,
    summary=(
        "these test functions are silently never run; pytest collects only "
        "module-level tests and Test* class methods named to the configured pattern"
    ),
    bite=lambda: (
        # Nested inside another function: the shape that silently ate 9 tests.
        Bites(
            "def helper():\n    def test_inner():\n        pass\n",
            "nested inside helper()",
        ),
        # A method of a class pytest will not collect from.
        Bites(
            "class Cases:\n    def test_inner(self):\n        pass\n",
            "not a module-level test",
        ),
        # Two levels deep inside a class, still invisible.
        Bites(
            "class TestOuter:\n    def helper(self):\n        def test_inner(self):\n            pass\n",
            "nested inside helper()",
        ),
        # A near-miss name: a direct method of a Test* class that pytest still never collects, because the
        # name does not match python_functions.
        Bites(
            "class TestThings:\n    def testresolve_near_miss(self):\n        pass\n",
            "collected pattern",
        ),
    ),
    clean=lambda: (
        "def test_module_level():\n    pass\n",
        "async def test_module_level_async():\n    pass\n",
        "class TestThings:\n    def test_method(self):\n        pass\n",
        "class TestThings:\n    async def test_async_method(self):\n        pass\n",
        # Fixtures and helpers are not tests and must not be reported.
        "class TestThings:\n    def helper_fixture(self):\n        pass\n",
        "def not_a_test_helper():\n    pass\n",
    ),
    scope=lambda *, exclude: trees_under(TESTS_DIR),
    proof_path=TESTS_DIR / "_synthetic.py",
)


# --------------------------------------------------------------------------- Alias-blindness: a dynamic
# pattern cannot rename itself past the chokepoint


DYNAMIC_RE_PATTERNS_ROUTE_THROUGH_SAFE_REGEX = Rule(
    name="dynamic-re-patterns-route-through-safe-regex",
    find=aliased_dynamic_pattern_re_offenders,
    summary=(
        "re.<method>(<dynamic pattern>) must go through app.safety.safe_regex, under any"
        " import alias -- the binding is resolved first, so an alias cannot rename a"
        " dynamic pattern past the ReDoS sweep"
    ),
    bite=lambda: (
        # The aliased spelling the name-anchored detector missed.
        Bites(
            "import re as _re\n\n\ndef f(p):\n    return _re.compile(p)\n",
            "_re.compile(<dynamic pattern>)",
        ),
        # The plain name stays covered: subsumption of the old detector.
        Bites("import re\n\n\ndef f(p):\n    return re.search(p, t)\n", "re.search(<dynamic pattern>)"),
        # The plain name stays covered: subsumption of the old detector.
        Bites("import re\n\n\ndef f(p):\n    return re.search(p, t)\n", "re.search(<dynamic pattern>)"),
    ),
    clean=lambda: (
        # A literal pattern under the same alias is safe.
        'import re as _re\n\n\ndef f():\n    return _re.compile(r"<loc>")\n',
        # No re import at all: nothing to check.
        "def f(p):\n    return compile(p)\n",
    ),
)


SANCTIONED_FUNCS_ARE_NOT_ALIASED_PAST_THE_SPELLING_LISTS = Rule(
    name="sanctioned-funcs-are-not-aliased-past-the-spelling-lists",
    find=aliased_sanctioned_func_offenders,
    summary=(
        "a sanctioned chokepoint function is called through an alias the name-matching"
        " detectors do not recognize; a missing cap or filter would be invisible to them"
    ),
    bite=lambda: Bites(
        "from app.transport.decompression import decompress_body as dec\n\n"
        "def f(raw, enc):\n    return dec(raw, enc)\n",
        "alias of the sanctioned decompress_body",
    ),
    clean=lambda: (
        # The spelling _DECOMPRESS_FUNCS already lists is known and silent.
        "from app.transport.decompression import decompress_body as _decompress_body\n\n"
        "def f(raw, enc):\n    return _decompress_body(raw, enc)\n",
        # An import that is not a sanctioned function is not this sweep's business.
        "from os import path as p\n\n\ndef f():\n    return p.join('a')\n",
    ),
)


TEST_DOUBLES_ROUTE_THE_FAILED_RESULT = Rule(
    name="test-doubles-route-the-failed-result",
    find=swallowing_test_double_offenders,
    summary=(
        "bind the production kwarg by name or forward **kwargs to the real callable; a"
        " double that drops it implements a contract that no longer exists. The"
        " failed_result refactor broke 24 tests silently through exactly this shape."
    ),
    scope=lambda *, exclude: trees_under(TESTS_DIR),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
    bite=lambda: Bites(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return {}\n",
        "run(**kwargs)",
    ),
    clean=lambda: (
        # Forwarding is the sanctioned shape: the double passes the contract through.
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return real.run(jobs, worker, on_result=on_result, **kwargs)\n",
        # Consulting kwargs by name (kwargs.get) also counts as routing it.
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return kwargs.get('x', 1)\n",
        # A non-Protocol method is not a double and stays unflagged.
        "def run(a, **kwargs):\n    return 1\n",
    ),
)


ERROR_FIELD_VALUES_ARE_TAXONOMY_MEMBERS = Rule(
    name="error-field-values-are-taxonomy-members",
    find=error_field_taxonomy_offenders,
    summary=(
        "error= must be a member of the declared CrawlError taxonomy (the field is"
        " documented as a stable code callers key on)"
    ),
    bite=lambda: Bites(
        'def f():\n    return terminal_crawl_result(url="u", error="worker_failed")\n',
        "not a Classification member",
    ),
    clean=lambda: (
        # A member literal passes.
        'def f():\n    return terminal_crawl_result(url="u", error="site_error")\n',
        # A classified variable is the sanctioned shape.
        "def f(c):\n    return terminal_crawl_result(url='u', error=c)\n",
    ),
)


def test_every_test_function_is_one_pytest_can_collect():
    EVERY_TEST_FUNCTION_IS_ONE_PYTEST_CAN_COLLECT.scan()


def test_uncollectible_test_detector_bites():
    """Every uncollectible shape fires; both collectible shapes stay clean."""
    EVERY_TEST_FUNCTION_IS_ONE_PYTEST_CAN_COLLECT.check_bites()


# =========================================================================== ALIAS-BLINDNESS (program 3):
# detectors must resolve import bindings, not match on a literal module name


def test_dynamic_pattern_re_calls_under_any_alias():
    """``import re as _re`` then ``_re.compile(<dynamic>)`` is invisible to a
    detector anchored on the name ``re``. The binding is resolved first, so an
    alias cannot rename a dynamic pattern past the chokepoint."""
    offenders = app_offenders(aliased_dynamic_pattern_re_offenders)
    assert not offenders, (
        "re.<method> with a dynamic pattern must go through app.safety.safe_regex, under any"
        f" import alias: {'; '.join(offenders)}"
    )


def test_alias_detector_bites():

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

    Resolving the import binding closes the one-alias-at-a-time gap: a new
    alias of ``decompress_body`` cannot name its way out of the cap requirement."""

    offenders = app_offenders(aliased_sanctioned_func_offenders)
    assert not offenders, (
        "a sanctioned chokepoint function is called through an alias the"
        " name-matching detectors do not recognize; a missing cap or filter would"
        f" be invisible to them: {'; '.join(offenders)}"
    )


def test_sanctioned_func_alias_detector_bites():
    """The alias audit fires on a spelling the hand-kept list does not contain,
    and stays silent on the two spellings the existing list already covers."""

    unknown = ast.parse(
        "from app.transport.decompression import decompress_body as dec\n\n"
        "def f(raw, enc):\n    return dec(raw, enc)\n"
    )
    hits = aliased_sanctioned_func_offenders(unknown, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "decompress_body" in hits[0], hits
    # The two spellings _DECOMPRESS_FUNCS already lists are known and silent.
    known = ast.parse(
        "from app.transport.decompression import decompress_body as _decompress_body\n\n"
        "def f(raw, enc):\n    return _decompress_body(raw, enc)\n"
    )
    assert not aliased_sanctioned_func_offenders(known, REPO_ROOT / "app" / "_synthetic.py")
    # An import that is not a sanctioned function is not this sweep's business.
    unrelated = ast.parse("from os import path as p\n\n\ndef f():\n    return p.join('a')\n")
    assert not aliased_sanctioned_func_offenders(unrelated, REPO_ROOT / "app" / "_synthetic.py")


# =========================================================================== TEST DOUBLES THAT SWALLOW
# SIGNATURE DRIFT (program 4) ===========================================================================


def test_doubles_do_not_accept_and_drop_kwargs():
    """A Protocol-shaped double that accepts ``**kwargs`` and never routes it is
    blind to a signature change on the production callable. The ``failed_result``
    refactor broke 24 tests silently through exactly this shape."""
    assert_trees_clean(
        swallowing_test_double_offenders,
        "bind the production kwarg by name or forward **kwargs to the real callable; a"
        " double that drops it implements a contract that no longer exists",
        trees=trees_under(TESTS_DIR),
    )


def test_failed_result_binding_detector_bites():
    """Bites: the shape that shipped, and the two ways the rule could go dead.

    Offender: a ``run`` with the scheduler's positionals plus ``**kwargs``, so
    the envelope cannot land; a binding ``run`` is the property asserted."""
    SCHEDULER_DOUBLES_BIND_FAILED_RESULT.check_bites()


def _scheduler_run_positional_names() -> tuple[str, ...]:
    """The real ``DomainScheduler.run`` positional names, in order.

    Read from production, not typed: the count and names tell a double from an
    unrelated ``run``, and a hand-kept tuple stales when the signature grows."""
    tree = ast.parse((REPO_ROOT / "app" / "orchestration" / "scheduler.py").read_text(encoding="utf-8"))
    for node in nodes_of(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            args = node.args
            return tuple(a.arg for a in args.posonlyargs + args.args if a.arg not in {"self", "cls"})
    raise AssertionError(
        "app/orchestration/scheduler.py no longer has a run(); the rule it backs is unverifiable"
    )


def _failed_result_binding_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A scheduler-shaped ``run()`` that does not bind ``failed_result``: the shape is
    the real positional count plus ``**kwargs`` -- mirrors the positionals and swallows
    the envelope -- leaving a plain utility ``run`` out of the report.
    """
    scheduler_shaped = _scheduler_run_positional_names()
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"):
            continue
        args = node.args
        positional = tuple(a.arg for a in args.posonlyargs + args.args if a.arg not in {"self", "cls"})
        if positional[: len(scheduler_shaped)] != scheduler_shaped:
            continue
        if args.kwarg is None:
            continue
        names = {a.arg for a in args.args + args.kwonlyargs} | {a.arg for a in args.posonlyargs}
        if "failed_result" not in names:
            offenders.append(offender_at(path, node, "run() does not bind failed_result by name"))
    return offenders


def _failed_result_binding_source() -> str:
    """The proof's violating ``run``, its positionals read from the real scheduler.

    Written, not typed, so the bite tracks the production signature: spelling
    the tuple keeps the proof green against a rule that stopped recognising it."""
    positionals = ", ".join(_scheduler_run_positional_names())
    return f"class _S:\n    async def run(self, {positionals}, **kwargs):\n        return {{}}\n"


SCHEDULER_DOUBLES_BIND_FAILED_RESULT = Rule(
    name="scheduler-doubles-bind-failed-result",
    find=_failed_result_binding_offenders,
    summary=(
        "the DomainScheduler.run contract requires failed_result; a double that omits it"
        " silently drops the envelope the pipeline supplies"
    ),
    premise=lambda: _scheduler_run_positional_names(),
    bite=lambda: Bites(_failed_result_binding_source(), "failed_result"),
    clean=lambda: (
        # The method that does bind the name is the property being asserted.
        (
            "class _S:\n    async def run(self, "
            + ", ".join(_scheduler_run_positional_names())
            + ", *, failed_result=None, **kwargs):\n"
            + "        return {}\n"
        ),
        # A ``run`` with no scheduler-shaped signature at all is not a scheduler double, so it is not the
        # property's business.
        "def run(a, b, **kwargs):\n    return 1\n",
    ),
    scope=lambda *, exclude: trees_under(TESTS_DIR),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def test_no_existing_double_swallows_failed_result():
    """Derived from the production signature, not a hand-kept list.

    Every scheduler double in tests/ binds ``failed_result`` by name today;
    a ``**kwargs``-only regression is caught by this AND the sweep above."""
    SCHEDULER_DOUBLES_BIND_FAILED_RESULT.scan()
