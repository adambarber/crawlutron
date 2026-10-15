"""Typing ratchet: records whose key set no type declares.

A pyright budget at zero says the annotations that are there are true. It says
nothing about the annotation that is absent, and that is where ``JobStatusView``
was ``dict[str, Any]`` -- twelve keys declared nowhere. The projection could stop
selecting one, the response model could stop declaring one, and
``JobStatusResponse(**view)`` into a model with ``extra="ignore"`` dropped the
difference without a word. It did: ``kind`` was selected on purpose, read by both
recovery seams, and had never reached a single response body. 2566 passing tests
could not see it, because no test asserts a field a response is silently missing.

The fix was one ``TypedDict``. This measures what keeps the next one from needing
one.

The measured property is a DECLARED KEY SET, not the presence of ``Any``.
``Any`` is not a type; it is an exit from the type system, and counting how many
times a token appears measures a spelling rather than a program. Three kinds of
annotation are in this tree and they are not the same thing:

    a domain record    a known set of keys with known value types. ``TypedDict``,
                       dataclass, or model. Declared. Not counted.
    a system type      a known CLASS with unconstrained contents -- ``JsonValue``
                       for a payload that really is arbitrary JSON, ``bytes``,
                       ``str``. "This could be anything" becomes "this is a JSON
                       payload", which is a real statement the checker composes
                       with. A dictionary is already the right shape for one of
                       these. Not counted.
    an exit            a record whose key set the code depends on and no
                       annotation records. This is the defect. Counted.

The honest ``JsonValue`` case and the defect are the same spelling on the page --
``dict[str, Any]`` -- and only the author knows which one it is. That is why
this is a ratchet and not a ban, and why the remedy names two moves rather than
one: declare a ``TypedDict`` if a key set exists, or name the payload as the
system type it is if it does not. Either one is a narrowing; deleting a token
without doing either is not.

``dict[str, str]`` and ``dict[str, int]`` are in neither, on purpose. An open key
set with a typed value IS the declaration of an open key set -- it is the honest
description of a header map, and there is no set of keys to declare.

What is given up, stated plainly: a budget does not stop the value that would
exceed it. It stops the population growing, names the work list in the failure
message, and makes every narrowing a commit that lowers the number. The way to
zero is mechanical and already demonstrated -- declare the record, and let the
compiler reject every key the declaration and its consumers disagree about.

To see the work list without failing anything:

    python -c "import sys; sys.path[:0]=['tests']; \\
      import tests.test_typing_budget as m; m.report()"
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

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
from tests.chokepoint_walks import nodes_of

_METRIC = "undeclared record"

_BUDGETS = {_METRIC: 208}

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
    """Whether this annotation's outermost type is a key-set-bearing record.

    Descends through subscripts and unions -- `list[dict[str, Any]]` and
    `dict[str, Any] | None` are records -- and stops at everything else, so a
    type constructor's own arguments are never mistaken for the annotation's type.
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
    """Every (metric, lineno, annotation) this tree offers, counted or not.

    Three annotated positions carry the type a caller is checked against: a
    parameter, a local or attribute assignment, and a return. `AnnAssign` is not
    a dataclass field declaration -- `dataclasses.field()` types are written in
    the class body as `x: T = field(...)`, which IS an AnnAssign, so those are
    included and deliberately so: a dataclass field is the single most-checked
    annotation in the tree.
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


def _walked() -> dict[str, int]:
    return {"annotation": sum(1 for _candidate in walk(candidates))}


test_collector_sees_the_whole_tree = collector_sees_the_whole_tree(
    _walked, {"annotation": 200}, subject="typing"
)


def test_the_collector_classifies_records_the_way_the_remedy_assumes() -> None:
    """The analogue of a bite proof for a corpus detector.

    Every other gate in this suite proves itself by planting a synthetic name and
    watching the detector catch it. A collector that walks a whole tree cannot do
    that -- `Rule` proofs accept one source string, which is why the corpus
    detectors stay hand-rolled. So the proof is the collector's own input: one of
    each thing it is supposed to count, and each of the things it must leave
    alone, including the case that must NOT be here.

    The negative half is the part that matters. The open-payload case and the
    defect share a spelling, so a collector that reported every `dict[str, Any]`
    as a defect would be unusable -- `JsonValue` is a legitimate answer and this
    gate must not drive the tree away from it. That the two are indistinguishable
    from the outside is the reason the remedy names both.
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
    """The record this gate was written for, checked at its own definition.

    Every other assertion in this file counts. This one names the specific claim
    the program rests on: `JobStatusView` is a `TypedDict`, not a `dict`. A
    future edit that relaxes it back to a bare record would leave the budget in
    this file unchanged -- the population would not grow, it would simply
    re-absorb the key set it had declared -- so the declaration itself has to be
    a tested fact rather than an assumption about the current tree.
    """
    from app.storage.job_store import JobStatusView

    assert issubclass(JobStatusView, dict), "a TypedDict is a dict at runtime, so this is the weak half"
    assert getattr(JobStatusView, "__required_keys__", None), "every key of the view must be mandatory"
    annotations = JobStatusView.__annotations__
    assert "kind" in annotations, "the key the defect dropped"
    assert "cap_truncated" in annotations, "the key a pydantic default was hiding behind"
    assert all(key in annotations for key in ("job_id", "status", "stats", "error"))
