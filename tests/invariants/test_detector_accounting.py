"""Count the invariant suite's detectors, so its coverage of itself is knowable.

WHAT IS BEING COUNTED
--------------------
A detector is a hand-written AST walk that answers "is this property still
true?". Being *found* by a walk is not the same as being *declared*, and this
module exists because the difference used to be invisible.

A walk wired as a ``Rule``'s ``find=`` carries a name, a summary that is the
string a reader actually sees when it fires, an explicit scope, and bite proofs
demonstrating the walk can still fire. An identical function left hand-rolled
carries none of that. Nothing states which property it pins, so a maintainer
cannot tell an enforced invariant from a leftover helper; nothing proves it is
still live, so deleting the code it was watching leaves a green suite.

WHY THE NUMBER IS COMPUTED, NOT WRITTEN DOWN
---------------------------------------------
An earlier revision opened this file with a MEASURED block -- "detector
functions 71, functions in PROPERTY_REGISTRY 3" -- and defined ``unaccounted``
as ``detectors - registered``. It was stale the moment the Rule migration
began, and it was wrong before that. A property-registry entry declares an
*owner module and symbols* for a *property*; a walk is a *checker*. Those are
not the same kind of thing, they are not 1:1 (one entry can cover several walks,
and several walks can cover one property), and subtracting one from the other
manufactures a number that looks like a gap and is not one. The old docstring
even conceded it "can overstate" the real gap.

A ``Rule``, by contrast, wraps exactly one walk. So ``detectors - declared`` is
an exact count, it moves the moment migration work lands, and it is the count
worth driving to zero. Call :func:`audit`. A written-down number would go stale
the same way, and a stale metric is worse than none because it is believed.

THE PROPERTY REGISTRY IS A SEPARATE PROGRAM
-------------------------------------------
That does not retire ``PROPERTY_REGISTRY``. Naming the owner of a property is
the mechanism that makes it drift-proof rather than merely checked:
``_property_owner_failures`` proves the owner is still its only definition. It
deserves registry entries for every property the suite pins. But it answers a
different question -- *whose job is this property* -- and its coverage should be
measured against the properties, not inferred by subtracting a count of
functions. That program wants to be done on its own terms, and counting
function definitions is not the instrument for it.

WHAT THIS MODULE IS, AND IS NOT
-------------------------------
It makes the migration gap a function call rather than an inference from
reading seventy-one walks by hand. That is the whole contribution, and it is
deliberately not a gate:

* **Not a cap on the detector count.** A cap invites the next person to add
  walks up to the cap and stop, which is the opposite of the intent.
* **Not an assertion that ``unaccounted == 0``.** That is red until the
  migration finishes, and the only honest ways to green it are to wire every
  walk to a rule (the work) or to lower the threshold until it fits today's
  number (a lie). It is reported, not enforced, for the same reason the
  registry gap was: a threshold picked to fit the present detects nothing and
  only ever gets re-raised.

THE BITES
---------
The bites exist because a walk that reads nothing is indistinguishable from a
suite with no detectors at all. One plants a detector in a tree the test owns
and proves the count moves by exactly one while a non-detector does not count;
the other proves a detector nested inside a factory is still seen, which is
the cheap way to add enforcement the suite cannot account for.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from tests.chokepoint_walks import REPO_ROOT, calls_to_any, from_imports, functions

INVARIANTS_ROOT = REPO_ROOT / "tests" / "invariants"

#: Every detector in the suite ends in this, which is the only reason the count
#: can be mechanical rather than a hand-maintained list -- a list would be a
#: second declaration of the tree, and would let a new walk land uncounted.
DETECTOR_SUFFIX = "_offenders"


@dataclass(frozen=True)
class DetectorAudit:
    """What the suite holds, and how much of it declares an owner."""

    detectors: tuple[str, ...]
    declared: tuple[str, ...]

    @property
    def unaccounted(self) -> int:
        """Detectors that no ``Rule`` names: no summary, no scope, no proof.

        This is the migration gap, and the only count here worth driving down.
        A hand-rolled walk enforces something real, but nothing states which
        property it pins, so a maintainer cannot tell an enforced invariant from
        a leftover helper, and nothing proves the walk can still fire.

        Unlike the registry comparison this one cannot overstate the gap. A
        detector is either wired to a ``Rule`` or it is not, so the arithmetic
        is exact; and it cannot go quietly stale, because a new ``_offenders``
        walk lands counted-but-undeclared until somebody gives it a rule.
        """
        return len(self.detectors) - len(self.declared)


def _rule_constructors(tree: ast.Module) -> set[str]:
    """The names in ``tree`` that call ``Rule``.

    Resolved through the module's own import so an aliased ``Rule`` still counts,
    with the bare name admitted unconditionally for a tree that never spells the
    import -- a planted fixture, or a module that re-exports the framework.
    """
    names = {"Rule"}
    for node in from_imports(tree):
        if (node.module or "").split(".")[-1] == "rules":
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "Rule")
    return names


def _wired_finds(tree: ast.Module) -> set[str]:
    """Detector names a ``Rule`` reaches through its ``find=``.

    The name may be the ``find`` itself or appear inside it. A detector needing
    an extra argument is wired ``find=lambda tree, path: _detector(tree, path,
    extra)``, and a rule that recognises only the bare name reports that walk as
    undeclared. That is not a small imprecision: it means the count tracks the
    spelling of the wiring rather than the wiring, so it reads the same before
    and after the migration it is supposed to measure.

    Restricted to the detector suffix so that a ``find`` expression mentioning
    some other helper does not declare a walk it never runs.
    """
    constructors = _rule_constructors(tree)
    wired: set[str] = set()
    for call in calls_to_any(tree, constructors):
        for keyword in call.keywords:
            if keyword.arg != "find":
                continue
            wired.update(
                inner.id
                for inner in ast.walk(keyword.value)
                if isinstance(inner, ast.Name) and inner.id.endswith(DETECTOR_SUFFIX)
            )
    return wired


def _survey(root: pathlib.Path) -> tuple[list[str], list[str]]:
    """Every walk under ``root``, and the subset a ``Rule`` names as its ``find``.

    Walks nested definitions as well as module-level ones: a detector built
    inside a factory runs for some inputs, and a top-level-only walk would not
    see it, which is precisely how enforcement ends up unaccounted for. Each
    file is parsed once and both answers come from that one tree.
    """
    detectors: list[str] = []
    declared: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wired = _wired_finds(tree)
        for node in functions(tree):
            if node.name.endswith(DETECTOR_SUFFIX):
                entry = f"{_display(path)}:{node.lineno} {node.name}"
                detectors.append(entry)
                if node.name in wired:
                    declared.append(entry)
    return detectors, declared


def detector_functions(root: pathlib.Path) -> list[str]:
    """Every ``*_offenders`` function under ``root``, as ``file:line name``."""
    return _survey(root)[0]


def declared_detectors(root: pathlib.Path) -> list[str]:
    """The subset of :func:`detector_functions` a ``Rule`` names as its ``find``."""
    return _survey(root)[1]


def _display(path: pathlib.Path) -> str:
    """Repo-relative where possible, absolute otherwise.

    The bites point this at trees under pytest's tmp dir, which are not inside
    the repo, so a bare ``relative_to`` raises and the failure reads as a
    defect in the walk rather than as a path that was never repo-relative.
    """
    if path.is_relative_to(REPO_ROOT):
        return str(path.relative_to(REPO_ROOT))
    return str(path)


def audit(root: pathlib.Path = INVARIANTS_ROOT) -> DetectorAudit:
    """Count the walks under ``root``, and the share of them a ``Rule`` names."""
    detectors, declared = _survey(root)
    return DetectorAudit(detectors=tuple(detectors), declared=tuple(declared))


def test_the_walk_sees_every_detector_in_the_suite() -> None:
    """The count is only useful if it is not vacuous on the real tree.

    A walk whose glob silently matched nothing would report zero detectors and
    zero unaccounted ones, and the number this module exists to produce would
    read as reassurance. So the real tree is asserted non-empty, and every
    entry is asserted to name a file that is actually under ``tests/invariants``
    -- a result from anywhere else means the walk escaped its scope.
    """
    found = detector_functions(INVARIANTS_ROOT)
    assert found, "the walk reported no detectors in tests/invariants/, so its counts prove nothing"
    assert all(entry.startswith("tests/invariants/") for entry in found), (
        f"the walk reported detectors outside tests/invariants/: "
        f"{[e for e in found if not e.startswith('tests/invariants/')]}"
    )
    assert all("_offenders" in entry for entry in found)


def test_planting_a_detector_moves_the_count_by_one(tmp_path: pathlib.Path) -> None:
    """The bite: one detector in, one counted; a near-miss function does not.

    Both halves matter. Without the first, a walk that returns nothing
    satisfies the count. Without the second, a walk that counts every function
    in the file would pass while never having looked at a detector at all --
    which is the failure mode a name-based count is supposed to rule out.
    """
    root = tmp_path / "invariants"
    root.mkdir()
    (root / "test_planted.py").write_text(
        "def _planted_offenders(tree, path):\n"
        "    return []\n"
        "\n"
        "\n"
        "def _not_a_detector(tree, path):\n"
        "    return []\n"
        "\n"
        "\n"
        "class _Holder:\n"
        "    def _method_offenders(self):\n"
        "        return []\n"
        "\n"
        "\n"
        "def test_something():\n"
        "    pass\n",
        encoding="utf-8",
    )
    found = detector_functions(root)
    assert len(found) == 2, f"expected the two planted detectors, got {found}"
    assert any(entry.endswith("_planted_offenders") for entry in found), found
    assert any(entry.endswith("_method_offenders") for entry in found), (
        f"a detector defined as a method went uncounted: {found}"
    )
    assert not any("_not_a_detector" in entry for entry in found), (
        f"a function that is not a detector was counted: {found}"
    )


def test_a_rule_backed_detector_counts_as_declared_and_a_hand_rolled_one_does_not(
    tmp_path: pathlib.Path,
) -> None:
    """The bite: being *found* by the walk is not the same as being *declared*.

    Before the Rule migration these were the same question, and the count
    answered "is there a walk here?". It no longer does. A detector wired as a
    ``Rule``'s ``find=`` carries a name, a summary, an explicit scope and bite
    proofs -- a maintainer reading one line learns which property is pinned and
    what firing it looks like. The identical function left hand-rolled carries
    none of that: nothing names the property, and nothing proves the walk can
    still fire.

    Measuring those two as one number is how the migration would have been able
    to report success without having done anything. The count that is worth
    having is the undeclared half, so this plants one of each and asserts the
    split. The second half is what stops the first from passing vacuously: a
    predicate that returned every detector would satisfy "the Rule-backed ones
    are declared" while quietly declaring the hand-rolled one too.

    Three wirings are planted, not one, because they are three spellings of the
    same decision and the first cut of this bite recognised only the first. A
    detector needing an extra argument is wired ``find=lambda tree, path:
    _detector(tree, path, extra)`` -- the rule still owns that walk, and a
    counter reading it as undeclared is not measuring the migration, it is
    measuring the spelling. That is the same category error the retired
    ``detectors - registered`` subtraction made, reintroduced one level down,
    and it is invisible unless a bite plants the lambda form.
    """
    root = tmp_path / "invariants"
    root.mkdir()
    (root / "test_planted.py").write_text(
        "from tests.rules import Rule\n"
        "\n"
        "\n"
        "def _direct_wired_offenders(tree, path):\n"
        "    return []\n"
        "\n"
        "\n"
        'DIRECT = Rule(name="direct", find=_direct_wired_offenders)\n'
        "\n"
        "\n"
        "def _lambda_wired_offenders(tree, path, extra=None):\n"
        "    return []\n"
        "\n"
        "\n"
        "LAMBDA = Rule(\n"
        '    name="lambda", find=lambda tree, path: _lambda_wired_offenders(tree, path, extra=1)\n'
        ")\n"
        "\n"
        "\n"
        "def _hand_rolled_offenders(tree, path):\n"
        "    return []\n"
        "\n"
        "\n"
        "def test_something():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = audit(root)
    assert len(result.detectors) == 3, f"expected all three planted detectors, got {result.detectors}"
    assert len(result.declared) == 2, (
        f"expected both Rule-backed detectors to be declared, got {result.declared}"
    )
    for wired in ("_direct_wired_offenders", "_lambda_wired_offenders"):
        assert any(entry.endswith(wired) for entry in result.declared), (
            f"{wired} is a Rule find but was not counted declared: {result.declared}"
        )
    assert not any("_hand_rolled" in entry for entry in result.declared), (
        f"a hand-rolled detector was reported as declared: {result.declared}"
    )
    assert result.unaccounted == 1, (
        f"the one hand-rolled detector is the whole migration gap; got {result.unaccounted}"
    )


def test_a_detector_nested_in_a_factory_is_still_counted(tmp_path: pathlib.Path) -> None:
    """A detector built at call time runs, so it has to be accounted for.

    Conditional or factory-defined walks are the cheap way to add enforcement
    the suite cannot see: the walk runs for whatever inputs reach it, and
    nothing counts it, so nothing notices when it stops running. The count
    therefore walks the whole tree rather than module-level definitions only.
    """
    root = tmp_path / "invariants"
    root.mkdir()
    (root / "test_nested.py").write_text(
        "def _build_detector():\n"
        "    def _nested_offenders(tree, path):\n"
        "        return []\n"
        "    return _nested_offenders\n"
        "\n"
        "\n"
        "if True:\n"
        "    def _conditional_offenders(tree, path):\n"
        "        return []\n",
        encoding="utf-8",
    )
    found = detector_functions(root)
    assert len(found) == 2, f"a non-module-level detector went uncounted; got {found}"
