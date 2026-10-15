"""Count the invariant suite's detectors, so its coverage of itself is knowable.

A detector is a hand-written AST walk; ``unaccounted`` (walks no ``Rule`` names)
is the migration gap, exact and worth zero. Deliberately reported, not enforced."""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from tests.chokepoint_walks import REPO_ROOT, calls_to_any, from_imports, functions

INVARIANTS_ROOT = REPO_ROOT / "tests" / "invariants"

DETECTOR_SUFFIX = "_offenders"


@dataclass(frozen=True)
class DetectorEntry:
    """One walk under ``tests/invariants``, wherever it is defined."""

    path: pathlib.Path
    lineno: int
    name: str

    def display(self) -> str:
        """Repo-relative where possible, absolute otherwise.

        Bite plants live under pytest's tmp dir (outside the repo), so a bare
        ``relative_to`` would raise on them."""
        if self.path.is_relative_to(REPO_ROOT):
            return str(self.path.relative_to(REPO_ROOT))
        return str(self.path)

    def __str__(self) -> str:
        return f"{self.display()}:{self.lineno} {self.name}"


@dataclass(frozen=True)
class DetectorAudit:
    """What the suite holds, and how much of it declares an owner."""

    detectors: tuple[DetectorEntry, ...]
    declared: tuple[DetectorEntry, ...]
    not_detectors: tuple[DetectorEntry, ...] = ()

    @property
    def unaccounted(self) -> int:
        """Detectors no ``Rule`` names: the migration gap, exact and worth zero."""
        return len(self.detectors) - len(self.declared)


def _rule_constructors(tree: ast.Module) -> set[str]:
    """The local names in ``tree`` that call ``Rule``.

    Resolved through the module's own import so an aliased ``Rule`` still
    counts; the bare name is admitted for a tree that never spells the import."""
    names = {"Rule"}
    for node in from_imports(tree):
        if (node.module or "").split(".")[-1] == "rules":
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "Rule")
    return names


def _wired_finds(tree: ast.Module) -> set[str]:
    """Detector names a ``Rule`` reaches through its ``find=``.

    The name may be the ``find`` itself or appear inside it; a lambda-wired
    detector must count, or the metric tracks the spelling, not the migration."""
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


def _survey(root: pathlib.Path) -> tuple[list[DetectorEntry], list[DetectorEntry], list[DetectorEntry]]:
    """Every detector walk under ``root``; the halves a ``Rule`` does and does not name.

    Nested definitions count too -- a factory-built detector runs for some
    inputs; each file parses once, both answers from that tree."""
    detectors: list[DetectorEntry] = []
    declared: list[DetectorEntry] = []
    not_detectors: list[DetectorEntry] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wired = _wired_finds(tree)
        for node in functions(tree):
            if not node.name.endswith(DETECTOR_SUFFIX):
                continue
            entry = DetectorEntry(path=path, lineno=node.lineno, name=node.name)
            if not _reads_a_tree(node):
                not_detectors.append(entry)
                continue
            detectors.append(entry)
            if node.name in wired:
                declared.append(entry)
    return detectors, declared, not_detectors


def _reads_a_tree(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function's first parameter names the tree it walks.

    Suffix alone does not decide it -- a factory can end in ``_offenders``;
    ``self``/``cls`` are skipped, so a method detector still counts."""
    if not node.args.args:
        return False
    positional = node.args.args
    if positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    if not positional:
        return False
    first = positional[0]
    if first.arg == "tree":
        return True
    annotation = ast.unparse(first.annotation) if first.annotation is not None else ""
    return "ast.AST" in annotation or "ast.Module" in annotation


def detector_functions(root: pathlib.Path) -> list[DetectorEntry]:
    """Every ``*_offenders`` walk under ``root``."""
    return _survey(root)[0]


def audit(root: pathlib.Path = INVARIANTS_ROOT) -> DetectorAudit:
    """Count the walks under ``root``, and the share a ``Rule`` names."""
    detectors, declared, not_detectors = _survey(root)
    return DetectorAudit(
        detectors=tuple(detectors),
        declared=tuple(declared),
        not_detectors=tuple(not_detectors),
    )


def test_the_walk_sees_every_detector_in_the_suite() -> None:
    """The count is only useful if it is not vacuous on the real tree.

    The real tree is asserted non-empty, and every entry must land under
    ``tests/invariants`` -- elsewhere means the walk escaped its scope."""
    found = detector_functions(INVARIANTS_ROOT)
    assert found, "the walk reported no detectors in tests/invariants/, so its counts prove nothing"
    assert all(entry.path.is_relative_to(INVARIANTS_ROOT) for entry in found), (
        f"the walk reported detectors outside tests/invariants/: "
        f"{[e for e in found if not e.path.is_relative_to(INVARIANTS_ROOT)]}"
    )
    assert all(entry.name.endswith(DETECTOR_SUFFIX) for entry in found)


def test_planting_a_detector_moves_the_count_by_one(tmp_path: pathlib.Path) -> None:
    """The bite: one detector in, one counted; a near-miss function does not.

    A method takes ``(self, tree, path)`` -- a receiver is not a tree -- but a
    method that DOES walk a tree still counts, so ``self`` is skipped."""
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
        "    def _method_offenders(self, tree, path):\n"
        "        return []\n"
        "\n"
        "\n"
        "def test_something():\n"
        "    pass\n",
        encoding="utf-8",
    )
    found = detector_functions(root)
    assert len(found) == 2, f"expected the two planted detectors, got {found}"
    assert any(entry.name == "_planted_offenders" for entry in found), found
    assert any(entry.name == "_method_offenders" for entry in found), (
        f"a detector defined as a method went uncounted: {found}"
    )
    assert not any(entry.name == "_not_a_detector" for entry in found), (
        f"a function that is not a detector was counted: {found}"
    )


def test_a_rule_backed_detector_counts_as_declared_and_a_hand_rolled_one_does_not(
    tmp_path: pathlib.Path,
) -> None:
    """The bite: being *found* by the walk is not the same as being *declared*.

    One of each is planted and the split asserted, with three wirings -- bare,
    aliased, lambda -- so the count measures the migration, not its spelling."""
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
        "def _documented_knob_offenders(documented, references):\n"
        "    return [name for name in sorted(documented - references)]\n"
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
        assert any(entry.name == wired for entry in result.declared), (
            f"{wired} is a Rule find but was not counted declared: {result.declared}"
        )
    assert not any(entry.name == "_hand_rolled_offenders" for entry in result.declared), (
        f"a hand-rolled detector was reported as declared: {result.declared}"
    )
    assert result.unaccounted == 1, (
        f"the one hand-rolled detector is the whole migration gap; got {result.unaccounted}"
    )
    assert not any(entry.name == "_documented_knob_offenders" for entry in result.detectors), (
        "a function that reads no tree was counted as a detector"
    )
    assert any(entry.name == "_documented_knob_offenders" for entry in result.not_detectors), (
        f"a non-detector was dropped rather than reported apart: {result.not_detectors}"
    )


def test_a_detector_nested_in_a_factory_is_still_counted(tmp_path: pathlib.Path) -> None:
    """A detector built at call time runs, so it has to be accounted for.

    The count walks the whole tree, not module-level definitions only, so a
    factory-defined walk cannot vanish undiscovered."""
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
