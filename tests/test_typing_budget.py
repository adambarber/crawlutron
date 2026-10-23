"""A pyright budget at zero says the annotations there are true, nothing about an
absent one -- ``JobStatusView`` was ``dict[str, Any]`` with twelve keys declared
nowhere, so a dropped key reached no response until one TypedDict fixed it.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from tests._ratchet import (
    budget_is_tight,
    budget_not_exceeded,
    collection_is_deterministic,
    collector_sees_the_whole_tree,
    walk,
)
from tests._ratchet import (
    report as ratchet_report,
)
from tests.chokepoint_walks import REPO_ROOT, nodes_of, trees_under

_METRIC = "undeclared record"
_ANY = "bare Any"

# 143 -> 137 (#135/#136); 134 -> 130 (#419: TypedDicts, fetch-phase and prose
# records). "bare Any" is #193's baseline: 161 -> 159 (#419, doors; JsonValue).
_BUDGETS = {_METRIC: 130, _ANY: 159}

#: The scripts/ tree, which #293 brings under this gate for the first time; its
#: measured baseline is declared first (value-ratchet rule).
_SCRIPTS_BUDGETS = {_METRIC: 16, _ANY: 0}

# A record is anything whose key set a caller might depend on.
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


def candidates(tree: ast.AST) -> Iterator[tuple[str, int, str]]:
    """Every (metric, lineno, annotation) this tree offers, counted or not. Three
    annotated positions carry what a caller is checked against; ``AnnAssign`` also
    covers dataclass fields (written `x: T = field(...)`), the most-checked type.
    """
    for node in nodes_of(tree):
        annotations: list[ast.expr] = []
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        for annotation in annotations:
            if annotation is None:
                continue
            source = ast.unparse(annotation)
            # The annotation itself, never its spine: a Callable's parameter Any
            # is not an exit (the record test below pins that distinction).
            if isinstance(annotation, ast.Name) and annotation.id == "Any":
                yield (_ANY, node.lineno, source)
            if _is_record(annotation) and (not _has_subscript(annotation) or _mentions_any(annotation)):
                yield (_METRIC, node.lineno, source)


def offenders(metric: str) -> list[Offender]:
    """The work list for `metric`, in tree order.

    Tree order because every entry is a position to edit, and a reader works
    down the list module by module rather than by how bad it is.
    """
    return [Offender(where, detail) for candidate, where, detail in walk(candidates) if candidate == metric]


def measurement(metric: str) -> int:
    """The number `metric` is compared against."""
    return len(offenders(metric))


def scripts_candidates(tree: ast.AST) -> Iterator[tuple[str, int, str]]:
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


scripts_offenders, scripts_measurement = _gate(REPO_ROOT / "scripts")


def report() -> None:
    ratchet_report(
        _BUDGETS,
        (measurement, offenders),
        lambda metric, shown, budget: (
            f"{metric:<18} {shown:>4} / budget {budget:<4} [{'OVER' if shown > budget else 'ok'}]"
        ),
    )


_REMEDY = (
    "Each of these is a record whose key set the code depends on and no annotation\n"
    "records. Two moves, and the second is as valid as the first:\n"
    "  * a key set exists -> declare it with a TypedDict, so a producer that stops\n"
    "    selecting a key becomes a build failure at the consumer;\n"
    "  * no key set exists -> say so as the system type it is (JsonValue for an\n"
    "    arbitrary JSON payload), which reads as a claim rather than an exit.\n"
    "Either one lowers this count, and this assertion enforces the rule on its own\n"
    "at zero. Deleting the token without doing either is not a narrowing."
)

test_budget_is_not_exceeded = budget_not_exceeded(
    _BUDGETS, (measurement, offenders), subject="typing", remedy=_REMEDY
)
test_budget_is_tight = budget_is_tight(_BUDGETS, (measurement, offenders), subject="typing")
test_collection_is_deterministic = collection_is_deterministic(
    _BUDGETS, (measurement, offenders), subject="typing"
)


_SCRIPTS_REMEDY = (
    "scripts/ was outside the typing budget before #293; its baseline is the 16 "
    "undeclared records it shipped with. Declare, or name the system type, and lower it."
)


test_scripts_budget_is_not_exceeded = budget_not_exceeded(
    _SCRIPTS_BUDGETS,
    (scripts_measurement, scripts_offenders),
    subject="scripts typing",
    remedy=_SCRIPTS_REMEDY,
)
test_scripts_budget_is_tight = budget_is_tight(
    _SCRIPTS_BUDGETS, (scripts_measurement, scripts_offenders), subject="scripts typing"
)
test_scripts_collection_is_deterministic = collection_is_deterministic(
    _SCRIPTS_BUDGETS, (scripts_measurement, scripts_offenders), subject="scripts typing"
)


def _assert_undeclared_record_fires() -> None:
    """The classifier fires on an undeclared record and not on a declared one."""
    defective = ast.parse("def load() -> dict[str, Any]: ...")
    assert list(candidates(defective))[:1] and list(candidates(defective))[0][0] == _METRIC
    clean = ast.parse("def load() -> dict[str, str]: ...")
    assert not list(candidates(clean))


def test_the_scripts_typing_budget_reaches_scripts() -> None:
    """The H1 reach: the typing budget now sweeps scripts/, which had none.
    The real tree shows it (16 records are counted, not invisible) and the shared
    classifier fires on a synthetic undeclared record, so one in scripts/ fires."""
    assert scripts_measurement(_METRIC) == 16, "the scripts typing budget stopped reaching the real tree"
    _assert_undeclared_record_fires()


#: The tests/ tree. It had a file-size and comment-length budget but no typing
#: budget; its measured baseline is declared first (value-ratchet rule).
_TESTS_BUDGETS = {_METRIC: 147, _ANY: 60}


suite_offenders, suite_measurement = _gate(REPO_ROOT / "tests")


_TESTS_REMEDY = (
    "tests/ had no typing budget before this cohort; its baseline is the 152 "
    "undeclared records it shipped with. Declare, or name the system type, and lower it."
)


test_tests_budget_is_not_exceeded = budget_not_exceeded(
    _TESTS_BUDGETS,
    (suite_measurement, suite_offenders),
    subject="tests typing",
    remedy=_TESTS_REMEDY,
)
test_tests_budget_is_tight = budget_is_tight(
    _TESTS_BUDGETS, (suite_measurement, suite_offenders), subject="tests typing"
)
test_tests_collection_is_deterministic = collection_is_deterministic(
    _TESTS_BUDGETS, (suite_measurement, suite_offenders), subject="tests typing"
)


def test_the_tests_typing_budget_reaches_tests() -> None:
    """The H1 reach: the typing budget now sweeps tests/, which had none. The real
    tree shows it (147 records are counted, not invisible) and the shared classifier
    fires on a synthetic undeclared record, so one in tests/ fires."""
    assert suite_measurement(_METRIC) == 147, "the tests typing budget stopped reaching the real tree"
    assert suite_measurement(_ANY) == 60, "the bare-Any budget stopped reaching the real tree"
    _assert_undeclared_record_fires()


def _walked() -> dict[str, int]:
    return {"annotation": sum(1 for _candidate in walk(candidates))}


# The walk's reach floor drops with the tree's declared records: the 137 #135
# leaves is the count itself, so the floor is a reach proof, not the ratchet.
test_collector_sees_the_whole_tree = collector_sees_the_whole_tree(
    _walked, {"annotation": 130}, subject="typing"
)


def test_the_collector_counts_a_bare_any_and_not_a_callables_spine() -> None:
    """#193's second metric bites on the position the record detector misses: a
    parameter annotated bare ``Any``; and does not count the ``Any`` inside a
    ``Callable``'s spine, which the record test above pins as honest."""
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
    from app.storage.job_store import JobStatusView

    assert issubclass(JobStatusView, dict), "a TypedDict is a dict at runtime, so this is the weak half"
    assert getattr(JobStatusView, "__required_keys__", None), "every key of the view must be mandatory"
    annotations = JobStatusView.__annotations__
    assert "kind" in annotations, "the key the defect dropped"
    assert "cap_truncated" in annotations, "the key a pydantic default was hiding behind"
    assert all(key in annotations for key in ("job_id", "status", "stats", "error"))
