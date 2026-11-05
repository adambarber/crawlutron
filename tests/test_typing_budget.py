"""The annotation ratchet: every declared record and bare ``Any`` is counted, and
zero means the tree's annotations are true -- nothing about an absent one, since
``JobStatusView`` was ``dict[str, Any]`` with twelve keys declared nowhere, so a
dropped key reached no response until one TypedDict fixed it.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from tests._ratchet import (
    _read,
    budget_is_tight,
    budget_not_exceeded,
    collection_is_deterministic,
    collector_sees_the_whole_tree,
    walk,
)
from tests._ratchet import (
    definition_counts as walked_definitions,
)
from tests._ratchet import (
    report as ratchet_report,
)
from tests.chokepoint_walks import REPO_ROOT, nodes_of, trees_under

_METRIC = "undeclared record"
_ANY = "bare Any"

# ALL batches landed (1-4): ZERO undeclared records, ZERO bare Any, no budget table left: the
# unconditional zero-assertions are the terminus.
_RECORD_ROOTS = frozenset(
    {"dict", "Dict", "list", "List", "tuple", "Tuple", "Mapping", "MutableMapping", "Sequence"}
)


@dataclass(frozen=True)
class Offender:
    where: str
    annotation: str

    def __str__(self) -> str:
        return f"{self.where} -> {self.annotation}"


def _mentions_any(node: ast.AST) -> bool:
    return any(isinstance(item, ast.Name) and item.id == "Any" for item in nodes_of(node))


def _is_record(node: ast.expr) -> bool:
    """Whether this annotation's outermost type is a key-set-bearing record, descending
    through subscripts and unions -- `list[dict[str, Any]]` and `dict[str, Any] | None`
    are records -- and stopping elsewhere so a type's arguments are never the type.
    """
    if isinstance(node, ast.Name):
        return node.id in _RECORD_ROOTS
    if isinstance(node, ast.Subscript):
        return _is_record(node.value)
    if isinstance(node, ast.BinOp):  # `X | Y`
        return _is_record(node.left) or _is_record(node.right)
    return False


def _has_subscript(node: ast.AST) -> bool:
    return any(isinstance(item, ast.Subscript) for item in nodes_of(node))


def candidates(tree: ast.AST) -> list[tuple[str, int, str]]:
    """Every (metric, lineno, annotation) this tree offers, counted or not. Three
    annotated positions carry what a caller is checked against; ``AnnAssign`` also
    covers dataclass fields (written `x: T = field(...)`), the most-checked type.
    """
    return list(_candidates(tree))


def _candidates(tree: ast.AST) -> Iterator[tuple[str, int, str]]:
    for node in nodes_of(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        annotations: list[ast.expr] = []
        if isinstance(node, ast.arg):
            arg_annotation: ast.expr | None = node.annotation
            if arg_annotation is not None:
                annotations.append(arg_annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            source = ast.unparse(annotation)
            # The annotation itself, never its spine: a Callable's parameter Any
            # is not an exit (the record test below pins that distinction).
            if isinstance(annotation, ast.Name) and annotation.id == "Any":
                yield (_ANY, lineno, source)
            if _is_record(annotation) and (not _has_subscript(annotation) or _mentions_any(annotation)):
                yield (_METRIC, lineno, source)


def offenders(metric: str) -> list[Offender]:
    """The work list for `metric`, in tree order.

    Tree order because every entry is a position to edit, and a reader works
    down the list module by module rather than by how bad it is.
    """
    found: list[Offender] = []
    for candidate, where, detail in walk(candidates):
        if candidate == metric:
            found.append(Offender(where, detail))
    return found


def measurement(metric: str) -> int:
    """The number `metric` is compared against."""
    return len(offenders(metric))


def scripts_candidates(tree: ast.AST) -> list[tuple[str, int, str]]:
    """Every candidate a scripts module offers, reusing the shared record detector."""
    return candidates(tree)


def _walk(root: Path) -> Iterator[tuple[str, str, str]]:
    """A whole root through the shared record classifier, labeled by module."""
    for path, tree in trees_under(root):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for candidate, line, source in candidates(tree):
            yield candidate, f"{relative}:{line}", source


def _offenders(walk, metric: str) -> list[Offender]:
    """The work list a root's walk offers, in tree order."""
    return [Offender(where, detail) for candidate, where, detail in walk if candidate == metric]


def _gate(root: Path) -> tuple[Callable[[str], list[Offender]], Callable[[str], int]]:
    """The (offenders, measurement) pair a root's typing gate reads."""

    def work(metric: str) -> list[Offender]:
        return _offenders(_walk(root), metric)

    def measure(metric: str) -> int:
        return len(work(metric))

    return work, measure


scripts_offenders, _scripts_measurement = _gate(REPO_ROOT / "scripts")


def report() -> None:
    def headline(metric: str, shown: int, budget: int) -> str:
        return f"{metric:<18} {shown:>4} / budget {budget:<4} [{'OVER' if shown > budget else 'ok'}]"

    ratchet_report({}, _read((measurement, offenders)), headline=headline)


test_collection_is_deterministic = collection_is_deterministic(
    {}, _read((measurement, offenders)), subject="typing"
)


_SCRIPTS_SEALED = (
    "undeclared record: the scripts tree is sealed at zero (#389) -- declare the "
    "record (TypedDict/model, see scripts/_jscpd_report_types.py) or name the "
    "system type it honestly is: "
)


def test_zero_undeclared_records_in_scripts() -> None:
    """scripts/ is sealed at zero: no budget row survives (#389).

    The classifier is the chokepoint -- an undeclared record or a bare Any is a
    hard failure that names its file and line; no sanction surface survives.
    """
    found = scripts_offenders(_METRIC) + scripts_offenders(_ANY)
    assert not found, _SCRIPTS_SEALED + "; ".join(str(o) for o in found)
    _assert_undeclared_record_fires()


def _assert_undeclared_record_fires() -> None:
    """The classifier fires on an undeclared record and not on a declared one."""
    defective = ast.parse("def load() -> dict[str, Any]: ...")
    assert list(candidates(defective))[:1] and list(candidates(defective))[0][0] == _METRIC
    clean = ast.parse("def load() -> dict[str, str]: ...")
    assert not list(candidates(clean))


def test_the_scripts_typing_budget_reaches_scripts() -> None:
    """The H1 reach, kept as the sealed gate's bite proof: the shared classifier
    fires on a synthetic undeclared record, so one introduced into scripts/ fails
    the sealed zero assertion above with its file and line named.
    """
    _assert_undeclared_record_fires()


#: Tests/ sealed at zero (#389): the last 3 were browser_blocking handler's
#: list[dict[str, Any]], now typed against the production InterceptedXhrRecord.
_TEST_BUDGETS = {_METRIC: 0, _ANY: 0}

suite_offenders, suite_measurement = _gate(REPO_ROOT / "tests")


_TESTS_REMEDY = (
    "tests/ records: declare the key set (a test-local TypedDict or the production "
    "model it mirrors), or name the system type it honestly is. Each declaration "
    "lowers the budget in `_TESTS_BUDGETS` in the same commit."
)


test_tests_budget_is_not_exceeded = budget_not_exceeded(
    _TEST_BUDGETS,
    _read((suite_measurement, suite_offenders)),
    subject="tests typing",
    remedy=_TESTS_REMEDY,
)
test_tests_budget_is_tight = budget_is_tight(
    _TEST_BUDGETS, _read((suite_measurement, suite_offenders)), subject="tests typing"
)
test_tests_collection_is_deterministic = collection_is_deterministic(
    _TEST_BUDGETS, _read((suite_measurement, suite_offenders)), subject="tests typing"
)


def test_the_tests_typing_budget_reaches_tests() -> None:
    """The H1 reach: the typing budget now sweeps tests/, which had none. The real
    tree shows it, and the shared classifier fires on a synthetic undeclared
    record, so one in tests/ fires.
    """
    assert suite_measurement(_METRIC) == 0, "the tests typing budget stopped reaching the real tree"
    assert suite_measurement(_ANY) == 0, "the bare-Any budget stopped reaching the real tree"
    _assert_undeclared_record_fires()


def _walked() -> dict[str, int]:
    # Reach is proved against real code, never the offender count: at the
    # terminus that count is ZERO, so a walk that saw nothing would lie.
    return walked_definitions()


# The walk's reach floor drops with the tree's declared records; the floor is
# a reach proof (walked must EXCEED it), not the ratchet itself.
test_collector_sees_the_whole_tree = collector_sees_the_whole_tree(
    _walked, {"function": 1000, "class": 200}, subject="typing"
)


def test_the_collector_counts_a_bare_any_and_not_a_callables_spine() -> None:
    """#193's second metric bites on the position the record detector misses: a
    parameter annotated bare ``Any``; and does not count the ``Any`` inside a
    ``Callable``'s spine, which the record test above pins as honest.
    """
    bare = ast.parse("def f(profile: Any) -> Any: ...")
    counted = {metric for metric, _line, _source in candidates(bare)}
    assert counted == {_ANY}, f"a bare Any must be its own counted defect: {counted}"

    spine = ast.parse("def g(fn: Callable[[Any, set[int]], Any]) -> None: ...")
    assert list(candidates(spine)) == [], "a Callable's spine is not an exit"

    declared = ast.parse("def h(profile: dict[str, str]) -> None: ...")
    assert list(candidates(declared)) == [], "a declared position is not an exit"


def test_the_collector_classifies_records_the_way_the_remedy_assumes() -> None:
    """A bite proof for a corpus detector: other gates plant a synthetic name, but a
    whole-tree walker cannot, so its input is its own proof -- including the open-
    payload case that must NOT be flagged (defect and ``JsonValue`` share a spelling).
    """
    parsed = ast.parse(
        "\n".join(
            [
                "def declared(view: JobStatusView) -> JobStatusView: ...",
                "def open_payload(body: JsonValue) -> JsonValue: ...",
                "def header_map(headers: dict[str, str]) -> None: ...",
                "def stats_bag(stats: dict[str, int]) -> None: ...[0]",
                "def concrete(url: str, port: int) -> None: ...",
                "def hook(fn: Callable[[int, set[int]], int]) -> None: ...[0]",
            ]
        ).replace("[0]", "")
    )
    measured = {source for _metric, _line, source in candidates(parsed)}
    assert measured == set(), f"collector flagged annotations that declare or are honest: {sorted(measured)}"

    # A `JsonValue` ALIASED as a dict is a declared record. This is the shape the remedy asks for, and the
    # collector must not keep counting it forever.
    aliased = ast.parse(
        "def view(v: dict[str, JsonValue]) -> None: ...\ndef w(v: dict[str, str]) -> None: ..."
    )
    assert list(candidates(aliased)) == [], "an annotation with a system type in it is not an exit"

    # The defect, in both of the forms the tree actually has it.
    defective = ast.parse(
        "def a(v: dict[str, Any]) -> None: ...\ndef b(v: list[dict[str, Any]] | None) -> None: ..."
    )
    assert {line for _metric, line, _source in candidates(defective)} == {1, 2}, "an exit must be counted"

    # A bare container declares nothing at all, and needs no `Any` to admit it.
    assert list(candidates(ast.parse("def a(v: dict) -> None: ..."))), "a bare dict is the purest case"

    # A callable's own arguments are not a record. `Callable[[Any, set[int]], Any]` mentions a set and is
    # still a callable.
    callable_only = ast.parse("def a(fn: Callable[[Any, set[int]], Any]) -> None: ...")
    assert list(candidates(callable_only)) == [], "spine, not every name inside the annotation"


def test_the_status_view_is_a_declared_record() -> None:
    """The record this gate was written for, at its own definition: relaxing it back
    to a bare record would leave the budget unchanged, re-absorbing the declared key
    set -- so the declaration itself is a tested fact, not an assumption.
    """
    from crawlutron.storage.job_store import JobStatusView

    assert issubclass(JobStatusView, dict), "a TypedDict is a dict at runtime, so this is the weak half"
    assert getattr(JobStatusView, "__required_keys__", None), "every key of the view must be mandatory"
    annotations = JobStatusView.__annotations__
    assert "kind" in annotations, "the key the defect dropped"
    assert "cap_truncated" in annotations, "the key a pydantic default was hiding behind"
    assert all(key in annotations for key in ("job_id", "status", "stats", "error"))
