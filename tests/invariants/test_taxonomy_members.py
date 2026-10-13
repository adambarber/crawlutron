"""Passed classifications, strategies, statuses and attempts are enum members."""

import ast
import pathlib

from tests.chokepoint_walks import (
    SANCTIONED,
    call_keyword_values,
    literal_keyword_membership_offenders,
    non_member_literals,
    offender_at,
    real_members,
)
from tests.rules import Bites, Rule

# This detector closes the class, not the instance: any classification handed to the primitives as a literal
# must be a CrawlClassification member.

_CLASSIFICATION_VALUE_SOURCES = ("classification", "known_classification")


_RESULT_PRIMITIVES = (
    # The generic sweep in tests/membership_gate.py reads the registry from the models, so a primitive added
    # tomorrow is covered the day it lands.
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
            r"add it to the enum (app/vocabulary/taxonomy.py) or route through the classifier",
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
        # The terminal-CrawlResult chokepoint is covered too: the render path builds terminal results through
        # it, so an unknown literal there reaches the wire through the same lenient dataclass that let
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


# --------------------------------------------------------------------------- Strategy-label validity

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
        # An invented member name -- exactly the shape that shipped on the browser worker's failure path.
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
        "def f(s):\n    return terminal_crawl_result(status=s)\n",
    ),
)


def test_result_primitives_emit_real_status_members():
    STATUSES_PASSED_ARE_ENUM_MEMBERS.scan()


def test_non_member_status_detector_bites():
    STATUSES_PASSED_ARE_ENUM_MEMBERS.check_bites()


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
