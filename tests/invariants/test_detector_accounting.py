"""Count the invariant suite's detectors, so its coverage of itself is knowable.

MEASURED 2026-09-30
--------------------
    detector functions in tests/invariants/   71
    functions in PROPERTY_REGISTRY             3

WHY THE SECOND NUMBER IS THE INTERESTING ONE
--------------------------------------------
A detector is a hand-written AST walk that answers "is this property still
true?". The registry is what turns that answer into a maintained invariant: a
registered property declares its owner module and owner symbols, and
``_property_owner_failures`` proves the owner is still the only definition of
it. That is the mechanism that makes a property drift-proof rather than merely
checked.

A detector outside the registry gets none of it. Nothing names the property it
guards, so nothing knows the property exists; when the owner moves and the
second definition appears, the walk keeps passing while guarding nothing, and
no gate notices. Sixty-eight of the seventy-one are in that position. That is
not a criticism of the walks -- most of them are careful, and several caught
real defects -- it is a statement that the suite cannot currently answer a
question a maintainer needs answered: *which of the properties this suite
pins have a declared owner, and which are being held only by the file they
happen to live in.*

WHAT THIS MODULE IS, AND IS NOT
-------------------------------
It makes the gap a function call rather than an inference from reading
seventy-one walks by hand. That is the whole contribution, and it is
deliberately not a gate:

* **Not a cap on the detector count.** A cap invites the next person to add
  walks up to the cap and stop, which is the opposite of the intent.
* **Not an assertion that detectors <= registry entries.** That inequality is
  red from the first run -- 71 against 3 -- and the only way to make it green
  honestly is to write seventy-one registry entries with a real owner and a
  real reason each, which is a program of work rather than a gate. Shipping it
  red, or shipping it with a threshold picked to fit today's number, would both
  be worse than not having it. The gap is stated here in prose and computed
  below; closing it is the next piece of work, and it wants to be done on its
  own terms.

THE BITES
---------
Both bites exist because a walk that reads nothing is indistinguishable from a
suite with no detectors at all. One plants a detector in a tree the test owns
and proves the count moves by exactly one while a non-detector does not count;
the other proves a detector nested inside a factory is still seen, which is
the cheap way to add enforcement the suite cannot account for.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from tests.chokepoint_walks import REPO_ROOT

INVARIANTS_ROOT = REPO_ROOT / "tests" / "invariants"

#: Every detector in the suite ends in this, which is the only reason the count
#: can be mechanical rather than a hand-maintained list -- a list would be a
#: second declaration of the tree, and would let a new walk land uncounted.
DETECTOR_SUFFIX = "_offenders"


@dataclass(frozen=True)
class DetectorAudit:
    """What the suite holds, and how much of it declares an owner."""

    detectors: tuple[str, ...]
    registered: int

    @property
    def unaccounted(self) -> int:
        """Detectors with no registry entry declaring the property they guard.

        An upper bound on the real gap, not the gap itself: a registry entry
        could in principle cover more than one detector, so this can overstate
        it. It cannot understate it, which is the direction that matters for a
        number used to decide where the work is.
        """
        return max(len(self.detectors) - self.registered, 0)


def detector_functions(root: pathlib.Path) -> list[str]:
    """Every ``*_offenders`` function under ``root``, as ``file:line name``.

    Walks nested definitions as well as module-level ones: a detector built
    inside a factory runs for some inputs, and a top-level-only walk would not
    see it, which is precisely how enforcement ends up unaccounted for.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.endswith(DETECTOR_SUFFIX):
                found.append(f"{_display(path)}:{node.lineno} {node.name}")
    return found


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
    """Count detectors under ``root`` against the registry.

    The registry is read at call time through the module the invariant tests
    import, rather than bound once at import, so a test that plants a
    detector sees the same path the suite would.
    """
    from tests.property_registry import PROPERTY_REGISTRY

    return DetectorAudit(detectors=tuple(detector_functions(root)), registered=len(PROPERTY_REGISTRY))


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
