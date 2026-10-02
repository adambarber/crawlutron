"""The result vocabularies have one home and one spelling.

Classification, strategy and status are closed enums with an order that is
itself meaning. A literal string, a re-imported alias or a shadowed table row
silently forks the vocabulary, so these checks close every route that is not
the taxonomy itself."""

from __future__ import annotations

import ast
import pathlib
import re

import httpx
import pytest

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    app_offenders,
    app_trees,
    assignments,
    bare_callee,
    call_keyword_values,
    call_nodes,
    functions,
    literal_keyword_membership_offenders,
    nodes_of,
    non_member_literals,
    offender_at,
    real_members,
    resolved_call_name,
)
from tests.rules import Bites, Rule

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

    assert (
        _table_shadowing_offenders(
            [((httpx.ConnectError,), "connection_error"), ((httpx.HTTPError,), "http_error")]
        )
        == []
    )


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


TRANSIENCE_VERDICT_HAS_ONE_HOME = Rule(
    name="transience-verdict-has-one-home",
    find=_ad_hoc_transience_offenders,
    summary=(
        "app.constants.is_transient_classification is the single answer for "
        "'is this failure worth another pass'"
    ),
    excludes=SANCTIONED | _TRANSIENCE_HOME_SANCTIONED,
    bite=lambda: Bites(
        'TRANSIENT_CLASSIFICATIONS = {"dns_error", "network_error"}\n',
        "re-answers the taxonomy's transience verdict",
    ),
    clean=lambda: (
        # Same shape spelled as a call: the detector keys on the collection-
        # valued assignment, and a ``frozenset()`` literal is a call, so a set-
        # shaped verdict name is expected to fire whether written as a literal
        # or as a call -- this one reads a ``Call`` value and so is not caught.
        "RETRYABLE_CLASSIFICATIONS = frozenset()\n",
        # A named alias to the taxonomy's own set is a re-export, not a second
        # opinion: that is the one legitimate spelling here.
        "from app.constants import TRANSIENT_CLASSIFICATIONS\n",
        "from app.constants import is_transient_classification as is_transient\n",
        "TRANSIENT_CLASSIFICATIONS = TRANSIENT_CLASSIFICATIONS\n",
        # A local *budget* is not a local *answer*: MAX_TRANSIENT_REQUEUES caps
        # how many times the recursive loop may re-try, which is legitimately
        # the caller's own number.
        "MAX_TRANSIENT_REQUEUES = 3\n",
        "RETRYABLE_ERROR_TYPES = frozenset()\n",
    ),
)


def test_transience_verdict_has_one_home():
    """No module outside app/constants.py may hold its own transient/retry set."""
    TRANSIENCE_VERDICT_HAS_ONE_HOME.scan()


def test_transience_home_detector_bites():
    """A local transience verdict fires; re-exports, budgets and count-shaped
    constants do not."""
    TRANSIENCE_VERDICT_HAS_ONE_HOME.check_bites()


# ---------------------------------------------------------------------------
# Error-handling chokepoint drift detectors
#
# A strategy that raises (rather than returning a classified result) falls to
# the scheduler's blanket envelope and is misclassified, or in on_result mode
# silently drops the page. Three invariants keep that from drifting back:
#
#   1. engine_result_from_exception is the only place a strategy exception may
#      become a result (no hand-rolled failed-result construction with a
#      hardcoded classification outside the two legitimate config guards).
#   2. BrowserPoolExhaustedError may only be caught by sanctioned sites
#      (the render endpoints, which own the 503 transport response) -- never
#      ad-hoc catch sites in the crawl path, which would re-derive retry
#      semantics locally instead of reading the classification.
#   3. classify_error_code must be defined in result_helpers (neutral home),
#      never in a strategy module; strategies reach for it, they don't own it.
# ---------------------------------------------------------------------------

# Classifications that a hand-rolled _failed_engine_result/build_engine_result
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
    for node in call_nodes(tree):
        if bare_callee(node) not in ("_failed_engine_result", "build_engine_result"):
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
            path, node, f"hand-rolled classification={status!r}; route through engine_result_from_exception"
        )


HAND_ROLLED_FAILED_RESULTS_GO_THROUGH_THE_CHOKEPOINT = Rule(
    name="hand-rolled-failed-results-go-through-the-chokepoint",
    find=_hand_rolled_failed_result_offenders,
    summary=(
        "failed results with a hardcoded classification must go through "
        "engine_result_from_exception (the single exception->result chokepoint)"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f():\n"
        "    return _failed_engine_result(\n"
        '        classification="network_error", error="worker_exception",\n'
        "    )\n",
        "route through engine_result_from_exception",
    ),
    clean=lambda: (
        # browser_crash is one of the engine's config guards -- no exception to
        # classify, the worker was never built -- so it may legitimately carry
        # a hardcoded code without going through the chokepoint.
        'def f():\n    return _failed_engine_result(\n        classification="browser_crash",\n    )\n',
    ),
)


def test_no_hand_rolled_failed_results_outside_the_chokepoint():
    HAND_ROLLED_FAILED_RESULTS_GO_THROUGH_THE_CHOKEPOINT.scan()


def test_hand_rolled_failed_result_detector_bites():
    HAND_ROLLED_FAILED_RESULTS_GO_THROUGH_THE_CHOKEPOINT.check_bites()


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


POOL_EXHAUSTION_CATCH_SITES_ARE_SANCTIONED = Rule(
    name="pool-exhaustion-catch-sites-are-sanctioned",
    find=_unsanctioned_pool_exhaustion_catch_offenders,
    summary=(
        "BrowserPoolExhaustedError may only be caught by the render transports (503 response) "
        "and the browser worker guard; the crawl path reads the browser_pool_exhausted classification"
    ),
    excludes=SANCTIONED | _POOL_EXHAUSTION_CATCH_SANCTIONED,
    bite=lambda: Bites(
        "async def f():\n"
        "    try:\n"
        "        await crawl()\n"
        "    except BrowserPoolExhaustedError:\n"
        "        return None\n",
        "read the classification instead",
    ),
    clean=lambda: (
        "async def f():\n"
        "    result = await crawl()\n"
        "    if result.classification == 'browser_pool_exhausted':\n"
        "        requeue()\n",
    ),
)


def test_pool_exhaustion_catch_sites_are_sanctioned():
    POOL_EXHAUSTION_CATCH_SITES_ARE_SANCTIONED.scan()


def test_pool_exhaustion_catch_detector_bites():
    POOL_EXHAUSTION_CATCH_SITES_ARE_SANCTIONED.check_bites()


def test_classifier_lives_in_the_neutral_home():
    """classify_error_code must be defined in result_helpers, not re-defined
    in a strategy module (the old site, http.py, forced browser.py to import
    across strategies)."""
    for path, tree in app_trees(exclude=SANCTIONED | _ERROR_CHOKEPOINT_SANCTIONED):
        for node in functions(tree):
            if node.name == "classify_error_code":
                raise AssertionError(
                    offender_at(path, node, "classify_error_code defined outside result_helpers")
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
    "build_engine_result",
    "engine_result_from_exception",
    "_skipped_engine_result",
    "crawl_result_from_exception",
    "terminal_crawl_result",
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


CLASSIFICATIONS_PASSED_ARE_ENUM_MEMBERS = Rule(
    name="classifications-passed-are-enum-members",
    find=_non_member_classification_offenders,
    summary=(
        "a literal classification passed to a result primitive must be a "
        "CrawlClassification member, or the strict enum at response shaping "
        "rejects the whole batch"
    ),
    excludes=SANCTIONED,
    bite=lambda: (
        Bites(
            'def f():\n    return _failed_engine_result(classification="totally_made_up")\n',
            "CrawlClassification member",
        ),
        # The terminal-CrawlResult chokepoint is covered too: the render path builds
        # terminal results through it, so an unknown literal there reaches the wire
        # through the same lenient dataclass that let url_budget_exceeded ship.
        Bites(
            'def f():\n    return terminal_crawl_result(classification="made_up_render_code")\n',
            "CrawlClassification member",
        ),
    ),
    clean=lambda: (
        "def f(cls):\n    return _failed_engine_result(classification=cls.url_budget_exceeded)\n",
        "def f(cls):\n    return terminal_crawl_result(classification=cls.site_error)\n",
    ),
)


def test_engine_emitted_classifications_are_enum_members():
    CLASSIFICATIONS_PASSED_ARE_ENUM_MEMBERS.scan()


def test_non_member_classification_detector_bites():
    CLASSIFICATIONS_PASSED_ARE_ENUM_MEMBERS.check_bites()


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


STRATEGIES_PASSED_ARE_ENUM_MEMBERS = Rule(
    name="strategies-passed-are-enum-members",
    find=_non_member_strategy_offenders,
    summary=(
        "a literal strategy passed to a result primitive must be a "
        "CrawlStrategy member, or None when no strategy was attempted; the "
        "strict enum on the wire model rejects anything else and the shaping "
        "fallback silently overwrites the real classification"
    ),
    excludes=SANCTIONED,
    bite=lambda: (
        # An invented member name -- exactly the shape that shipped on the browser
        # worker's failure path.
        Bites(
            'def f():\n    return engine_result_from_exception(strategy="BROWSER_DECLARED_CRAWLER")\n',
            "CrawlStrategy member",
        ),
        # The empty string is the same lie in cheaper clothing.
        Bites(
            'def f():\n    return _failed_engine_result(strategy="")\n',
            "CrawlStrategy member",
        ),
    ),
    clean=lambda: (
        # None is admitted: it is the honest "no strategy attempted" label.
        "def f():\n    return _failed_engine_result(strategy=None)\n",
        # A real member literal and a non-literal both pass without comment.
        'def f():\n    return _skipped_engine_result(strategy="BROWSER_HEADLESS")\n',
        "def f(s):\n    return crawl_result_from_exception(strategy=s)\n",
    ),
)


def test_engine_emitted_strategies_are_enum_members():
    STRATEGIES_PASSED_ARE_ENUM_MEMBERS.scan()


def test_non_member_strategy_detector_bites():
    STRATEGIES_PASSED_ARE_ENUM_MEMBERS.check_bites()


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
        advice=("add it to the Literal (app/models/taxonomy.py) or derive it from an existing member"),
    )


STATUSES_PASSED_ARE_ENUM_MEMBERS = Rule(
    name="statuses-passed-are-enum-members",
    find=_non_member_status_offenders,
    summary=(
        "a literal status passed to a result primitive must be a ResultStatus "
        "member, or the strict enum on the wire model rejects the batch"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        'def f():\n    return terminal_crawl_result(status="never_a_status")\n',
        "ResultStatus member",
    ),
    clean=lambda: (
        'def f():\n    return terminal_crawl_result(status="failed")\n',
        # A non-literal (a variable or the caller's own status field) passes.
        "def f(s):\n    return build_engine_result(status=s)\n",
    ),
)


def test_result_primitives_emit_real_status_members():
    STATUSES_PASSED_ARE_ENUM_MEMBERS.scan()


def test_non_member_status_detector_bites():
    STATUSES_PASSED_ARE_ENUM_MEMBERS.check_bites()


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


CRAWL_STRATEGY_ATTEMPT_SITES_USE_REAL_MEMBERS = Rule(
    name="crawl-strategy-attempt-sites-use-real-members",
    find=_non_member_attempt_strategy_offenders,
    summary=(
        "a literal strategy at a CrawlStrategyAttempt construction site must be a "
        "CrawlStrategyName member, or None when no strategy was attempted"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_DECLARED_CRAWLER', status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n",
        "CrawlStrategyName member",
    ),
    clean=lambda: (
        # None is admitted -- the honest "no strategy attempted" label.
        "def f():\n    return CrawlStrategyAttempt(strategy=None, status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n",
        # A real member literal and a non-literal both pass.
        "def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_HEADLESS')\n",
        "def f(s):\n    return CrawlStrategyAttempt(strategy=s)\n",
    ),
)


def test_crawl_strategy_attempt_sites_usereal_members():
    CRAWL_STRATEGY_ATTEMPT_SITES_USE_REAL_MEMBERS.scan()


def test_crawl_strategy_attempt_detector_bites():
    CRAWL_STRATEGY_ATTEMPT_SITES_USE_REAL_MEMBERS.check_bites()


def test_crawl_strategy_name_members_match_constants():
    """The ``CrawlStrategyName`` Literal must not drift from ``app.constants``.

    ``CrawlStrategyName`` cannot reference the ``Final`` constants directly inside
    ``Literal[...]`` (mypy rejects that), so its members are spelled as string
    literals. constants.py remains the single source of truth; this pins the two
    together so a rename there cannot orphan the alias.
    """
    from typing import get_args

    from app import constants
    from app.models.taxonomy import CrawlStrategyName

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
    ``check_literal_fields`` rejects the whole batch at construction because the
    Literal never heard of it. The reverse -- a Literal member with no enum
    member -- reaches the strict enum at response shaping and takes the batch
    down there instead.

    Both directions are asserted, so neither half of the drift can be the one
    that slips through.
    """
    from typing import get_args

    from app.constants import CrawlClassification
    from app.models.taxonomy import Classification

    declared = set(get_args(Classification))
    enum = {member.value for member in CrawlClassification}
    assert enum - declared == set(), (
        "every CrawlClassification member needs a Classification Literal member:"
        f" missing {sorted(enum - declared)} (app/models/taxonomy.py)"
    )
    assert declared - enum == set(), (
        "a Classification member with no CrawlClassification member is downgraded"
        f" to site_error at response shaping: unknown {sorted(declared - enum)}"
    )


def test_crawl_error_is_the_pipeline_additions_over_classification():
    """The pipeline-only codes are a real split, not a second copy of the taxonomy.

    ``CrawlError`` is defined as the union of ``Classification`` and
    ``PIPELINE_ONLY_CRAWL_ERRORS`` (app/models/taxonomy.py), so its membership is derived
    and cannot drift. What a union cannot check is whether the split is still
    meaningful: a code listed as a pipeline condition while also being a
    classification says a page outcome is a pipeline condition, and a pipeline
    condition deleted silently narrows the ``error`` field for callers who key on
    it as a stable code.
    """
    from app.models.taxonomy import PIPELINE_ONLY_CRAWL_ERRORS, Classification, literal_members

    pipeline_only = literal_members(PIPELINE_ONLY_CRAWL_ERRORS)
    classification = literal_members(Classification)
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
# sweep: the (dataclass, field, Literal) triples are read from app/models at
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
    offenders = app_offenders(generic_literal_field_offenders, members=field_members)
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
    hits = generic_literal_field_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    assert len(hits) == 1 and "totally_made_up" in hits[0], hits
    # A real member literal passes.
    good = ast.parse('def f():\n    return _failed_engine_result(classification="network_error")\n')
    assert not generic_literal_field_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A non-literal (the shape a correct caller writes) passes without comment.
    var = ast.parse("def f(c):\n    return _failed_engine_result(classification=c)\n")
    assert not generic_literal_field_offenders(var, REPO_ROOT / "app" / "_synthetic.py", members=members)


def test_generic_membership_subsumes_the_per_field_detectors():
    """The generic sweep covers every field the three per-field detectors
    guarded, and every primitive the hard-coded list forgot.

    The historical detectors matched on a *transcribed* primitive list of six
    names (``_RESULT_PRIMITIVES`` below). Three live primitives that set
    classification/status/strategy were never on it -- ``binary_no_text_result``
    (which sets all three), ``crawl_result_from_engine_result``, and
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
        "build_engine_result",
        "engine_result_from_exception",
        "_skipped_engine_result",
        "crawl_result_from_exception",
        "terminal_crawl_result",
    }
    missed = RESULT_PRIMITIVES - transcribed
    assert (
        missed
        == {
            "binary_no_text_result",
            "crawl_result_from_engine_result",
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

    from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt
    from app.models.engine_crawl_result import EngineCrawlResult
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
        attempt=CrawlStrategyAttempt(
            strategy="BROWSER_HEADLESS",
            status="success",
            classification="rendered",
            http_status=200,
            content_type=None,
            elapsed_ms=0,
        ),
        final_url=None,
        html=None,
        text=None,
        attempts=1,
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
    # The axis lives on the attempt the result holds, so that is where the
    # override is spelled -- reaching for a top-level `error=` would be a
    # constructor argument this record does not have.
    with pytest.raises(ValueError, match=r"\.error="):
        CrawlStrategyAttempt(**{**member_crawl_result_fields["attempt"], "error": "worker_failed"})


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
    from tests.membership_gate import literal_typed_field_members, namespace_constant_offenders

    field_members = literal_typed_field_members()
    offenders = app_offenders(namespace_constant_offenders, members=field_members)
    assert not offenders, (
        "a constant from a foreign namespace was consumed as a taxonomy member; the"
        " value collides with a real member which is exactly why a value check misses"
        f" it: {'; '.join(offenders)}"
    )


def test_wrong_namespace_detector_bites():
    """The provenance detector fires on the historical shape and admits the
    correct one."""
    from tests.membership_gate import literal_typed_field_members, namespace_constant_offenders

    members = literal_typed_field_members()
    bad = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f():\n"
        "    return _failed_engine_result(classification=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    hits = namespace_constant_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    assert len(hits) == 1 and "EXTRACTION_METHOD_" in hits[0], hits
    # The same constant used as its own field is the intended use.
    ok = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f(m):\n"
        "    return build_engine_result(extraction_method=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    assert not namespace_constant_offenders(ok, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A parameter (what a correct caller writes) is not a constant: passes.
    param = ast.parse("def f(cls):\n    return _failed_engine_result(classification=cls.site_error)\n")
    assert not namespace_constant_offenders(param, REPO_ROOT / "app" / "_synthetic.py", members=members)


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
            where="http.http_strategy_result",
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
            where="http.http_strategy_result",
            members=members,
        )


# ---------------------------------------------------------------------------
# The construction check has exactly one owner
#
# ``check_literal_fields`` is a module-level function and the result dataclasses
# each had to reach it through their own ``__post_init__``. That trampoline was
# the second thing to keep in sync: the check is one function, but a result
# dataclass added tomorrow would validate nothing while reading exactly like the
# ones that do. It is now inherited from ``models.LiteralChecked``, so the check
# is part of the class rather than something it remembers to ask for -- and
# "inherited" is a claim the tree can check, which is what these two do:
# a caller of the check outside the owner is a second copy of it, and a result
# dataclass that does not inherit the owner is a class with no check.
# ---------------------------------------------------------------------------


def _literal_check_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Any call to ``check_literal_fields`` outside its one owning ``__post_init__``."""
    offenders: list[str] = []
    for node in call_nodes(tree):
        if resolved_call_name(tree, node) != "app.models.literal_checked.check_literal_fields":
            continue
        owner = _enclosing_class(tree, node)
        if owner is not None and owner.name == "LiteralChecked":
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


THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER = Rule(
    name="the-literal-field-check-is-called-by-one-owner",
    find=_literal_check_offenders,
    summary=(
        "check_literal_fields is called outside models.LiteralChecked, so the"
        " construction-time taxonomy check exists in more than one place"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "from app.models.literal_checked import check_literal_fields\n"
        "@dataclass(frozen=True)\n"
        "class CrawlResult:\n"
        "    status: str\n"
        "    def __post_init__(self) -> None:\n"
        "        check_literal_fields(self)\n",
        "literal-check-called-outside-its-owner",
    ),
    clean=lambda: (
        # The class that is allowed to call it: the owning base class itself.
        "from app.models.literal_checked import check_literal_fields\n"
        "class LiteralChecked:\n"
        "    def __post_init__(self) -> None:\n"
        "        check_literal_fields(self)\n",
        # A module that never mentions the check is nobody else's business.
        "from app.models.crawl_result import CrawlResult\n"
        "def build() -> CrawlResult:\n    return CrawlResult()\n",
    ),
)


def test_the_literal_field_check_is_called_by_one_owner():
    """One caller for ``check_literal_fields``, and it is the owning base class.

    A second caller is a second place the construction check can be re-spelled,
    which is the shape this consolidation removed: the check was one function
    reached through three identical trampolines, and a class that re-spelled it
    would be indistinguishable from one that inherited it.
    """
    THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER.scan()


def test_literal_check_owner_detector_bites():
    """The owner guard fires on a re-spelled trampoline and admits the base.

    The first shape is the pre-consolidation one -- a class calling the check for
    itself -- and the second is the class that is allowed to. The third is the
    shape the guard must NOT fire on: a *different* module's function that
    happens to share the name, resolved by import bindings rather than by text.
    """
    THE_LITERAL_FIELD_CHECK_IS_CALLED_BY_ONE_OWNER.check_bites()


def test_every_taxonomy_literal_dataclass_inherits_the_construction_check():
    """A dataclass that declares a taxonomy ``Literal`` field inherits the check.

    The half of the property the owner guard cannot see: an *omission* rather
    than a duplicate. Which dataclasses qualify is derived from the taxonomy's
    own declaration -- ``LITERAL_FIELDS`` for the field names and
    ``literal_members`` for whether the annotation is really a taxonomy alias --
    and the classes are enumerated by walking ``app/models``, so a result
    dataclass added tomorrow is covered the day it lands and nothing here is a
    hand-kept class list. A field merely *named* like a taxonomy field but
    annotated ``str`` is not one of these and is not reported.

    Splitting the declarations into one file per type used to narrow this: it
    could only see the classes that shared a module with the aliases, and the
    docstring said so and called the owner guard above the tree-wide half. The
    package made that gap disappear -- the aliases and the dataclasses now share
    a HOME rather than a file -- so this guard is the whole property and the
    caveat it used to carry is what makes it worth keeping the enumeration
    derived: ``model_index`` finds every declared class by walking the package.
    """
    import typing
    from dataclasses import is_dataclass

    from app.models.literal_checked import LITERAL_FIELDS, LiteralChecked
    from app.models.taxonomy import literal_members
    from tests.model_index import declared_types

    checked: dict[str, list[str]] = {}
    for name, cls in sorted(declared_types().items()):
        if not (isinstance(cls, type) and is_dataclass(cls)):
            continue
        # Field names first: a dataclass with no taxonomy-named field cannot be
        # one of these, and asking for its resolved hints would evaluate a
        # TYPE_CHECKING-only annotation (CrawlJob's HostPolicy) for nothing.
        named = [field for field in cls.__dataclass_fields__ if field in LITERAL_FIELDS]
        if not named:
            continue
        hints = typing.get_type_hints(cls)
        fields = [field for field in named if literal_members(hints.get(field))]
        if not fields:
            continue
        checked[name] = fields
        assert issubclass(cls, LiteralChecked), (
            f"{name} declares the taxonomy Literal field(s) {sorted(fields)} but no"
            " construction check; inherit LiteralChecked so a non-member is"
            " rejected when the value is built rather than at the wire"
        )
    assert checked, (
        "no taxonomy Literal field was enumerated, so the sweep is a no-op:"
        f" LITERAL_FIELDS={sorted(LITERAL_FIELDS)}"
    )
    # CrawlResult is deliberately ABSENT. It no longer declares a taxonomy
    # Literal of its own: the render result holds the attempt it was rendered
    # by, and status/classification/error/selected_strategy are properties
    # reaching into it, so the record that declares those axes and the record
    # that checks them are the same object. Naming CrawlResult here would be
    # asserting a field it no longer has.
    assert set(checked) == {"CrawlStrategyAttempt", "EngineCrawlResult"}, sorted(checked)
