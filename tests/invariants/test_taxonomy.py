from __future__ import annotations

import ast
import pathlib
import re

from tests.chokepoint_walks import (
    SANCTIONED,
    app_trees,
    assignments,
    bare_callee,
    call_nodes,
    functions,
    nodes_of,
    offender_at,
)
from tests.rules import Bites, Registry, Rule

"""Each taxonomy artifact has one home and one spelling."""


# --------------------------------------------------------------------------- The ordered classification
# table: order is semantics, so it is checked
_CATCH_ALL_EXCEPTIONS: tuple[type[BaseException], ...] = (ValueError, OSError, Exception)


def _table_shadowing_offenders(rows) -> list[str]:
    """Report rows that can never fire, and broad rows that are not terminal.

    First-hit-wins means a row is dead once an earlier row's type is a
    superclass of one of its own, regardless of spelling.
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
    from app.orchestration.classification import CLASSIFICATION_BY_EXCEPTION

    offenders = _table_shadowing_offenders(CLASSIFICATION_BY_EXCEPTION)
    assert not offenders, (
        "the ordered table is matched first-hit-wins, so these rows can never fire "
        "or would swallow later verdicts -- order the specific rows first: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- Classification transience: one
# verdict, owned by the taxonomy ---------------------------------------------------------------------------
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


# A named alias to the taxonomy's own set is a re-export, not a second opinion: that is the one legitimate
# spelling here.
_TRANSIENCE_REEXPORTS = frozenset(
    {
        "TRANSIENT_CLASSIFICATIONS",
        "NON_TRANSIENT_CLASSIFICATIONS",
        "is_transient_classification",
    }
)


def _transience_verdict_name(target: ast.expr, value: ast.expr | None) -> str | None:
    """The name of a module-level transience/retry verdict, or ``None``.

    The shape's three requirements (verdict name, collection value, not a
    re-export of the taxonomy's own set) are one predicate, stated once here.
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
    """Flag a module-level transience/retry verdict defined outside the taxonomy.

    A consumer must call the taxonomy's classifier; a second opinion is what
    made ``dns_error`` look retryable while ``connection_error`` did not.
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
        "app.vocabulary.taxonomy.is_transient_classification is the single answer for "
        "'is this failure worth another pass'"
    ),
    excludes=SANCTIONED | _TRANSIENCE_HOME_SANCTIONED,
    bite=lambda: Bites(
        'TRANSIENT_CLASSIFICATIONS = {"dns_error", "network_error"}\n',
        "re-answers the taxonomy's transience verdict",
    ),
    clean=lambda: (
        "RETRYABLE_CLASSIFICATIONS = frozenset()\n",
        # A named alias to the taxonomy's own set is a re-export, not a second opinion: that is the one
        # legitimate spelling here.
        "from app.vocabulary.taxonomy import TRANSIENT_CLASSIFICATIONS\n",
        "from app.vocabulary.taxonomy import is_transient_classification as is_transient\n",
        "TRANSIENT_CLASSIFICATIONS = TRANSIENT_CLASSIFICATIONS\n",
        # A local *budget* is not a local *answer*: MAX_TRANSIENT_REQUEUES caps how many times the recursive
        # loop may re-try, which is legitimately the caller's own number.
        "MAX_TRANSIENT_REQUEUES = 3\n",
        "RETRYABLE_ERROR_TYPES = frozenset()\n",
    ),
)


# --------------------------------------------------------------------------- Classification transience: one
# verdict, owned by the taxonomy ---------------------------------------------------------------------------
_ERROR_CHOKEPOINT_SANCTIONED = frozenset({"classification.py"})


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
        if bare_callee(node) not in ("failed_engine_result", "build_engine_result"):
            continue
        status = None
        for kw in node.keywords:
            if kw.arg == "classification" and isinstance(kw.value, ast.Constant):
                status = kw.value.value
        if status in (None,) or status in _NON_EXCEPTION_CLASSIFICATIONS:
            continue
        # The engine's unsupported-strategy guard and the scheduler's circuit-breaker guard are policy
        # decisions, not caught exceptions: they carry their own taxonomy code in the error field.
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
        "    return failed_engine_result(\n"
        '        classification="network_error", error="worker_exception",\n'
        "    )\n",
        "route through engine_result_from_exception",
    ),
    clean=lambda: (
        'def f():\n    return failed_engine_result(\n        classification="browser_crash",\n    )\n',
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


def test_status_resolver_lives_in_the_vocabulary_home():
    """status_for_classification is defined once, in the taxonomy module."""
    for path, tree in app_trees(exclude=SANCTIONED | frozenset({"taxonomy.py"})):
        for node in functions(tree):
            if node.name == "status_for_classification":
                raise AssertionError(
                    offender_at(path, node, "status_for_classification defined outside the taxonomy module")
                )


def test_classifier_lives_in_the_neutral_home():
    """classify_error_code must be defined in the classification module, not
    re-defined in a strategy module (the old site, http.py, forced browser.py
    to import across strategies)."""
    for path, tree in app_trees(exclude=SANCTIONED | _ERROR_CHOKEPOINT_SANCTIONED):
        for node in functions(tree):
            if node.name == "classify_error_code":
                raise AssertionError(
                    offender_at(path, node, "classify_error_code defined outside the classification module")
                )


RULES = Registry(
    (
        TRANSIENCE_VERDICT_HAS_ONE_HOME,
        HAND_ROLLED_FAILED_RESULTS_GO_THROUGH_THE_CHOKEPOINT,
        POOL_EXHAUSTION_CATCH_SITES_ARE_SANCTIONED,
    )
)


def test_no_rule_violations():
    """One transience home, failed results chokepointed, exhaustion catches sanctioned."""
    RULES.check_all_clean()


# --------------------------------------------------------------------------- Domain-health accounting: the
# scheduler reads the taxonomy's one table, never a second declaration


def test_scheduler_reads_the_taxonomy_domain_health_table():
    """What a status charges the domain's health book is declared once.

    #167: the scheduler hand-wrote the table, so a taxonomy change never reached it.
    Identity pins the scheduler to the taxonomy's single object.
    """
    import typing

    import app.orchestration.scheduler as scheduler
    from app.vocabulary import taxonomy

    members = set(typing.get_args(taxonomy.ResultStatus))
    assert scheduler.DOMAIN_HEALTH_BY_STATUS is taxonomy.DOMAIN_HEALTH_BY_STATUS, (
        "the scheduler declares its own domain-health accounting; read the taxonomy's table"
    )
    assert not members - set(taxonomy.DOMAIN_HEALTH_BY_STATUS), (
        f"a ResultStatus member has no domain-health verdict: "
        f"{sorted(members - set(taxonomy.DOMAIN_HEALTH_BY_STATUS))}"
    )
    assert not set(taxonomy.DOMAIN_HEALTH_BY_STATUS) - members, (
        "the accounting table names a status the taxonomy does not define: "
        f"{sorted(set(taxonomy.DOMAIN_HEALTH_BY_STATUS) - members)}"
    )
