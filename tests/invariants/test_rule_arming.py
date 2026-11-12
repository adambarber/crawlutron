"""Is every rule still ARMED on a name the app tree actually has?

Move a literal name and the comparisons stop matching -- the rule reports green
forever; bite proofs are synthetic, so the ``find=`` underscore keys are audited."""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator, Sequence

from tests.chokepoint_walks import REPO_ROOT, parsed
from tests.invariants._armed_detectors import ArmedHandRolled, hand_rolled_detectors
from tests.invariants._shared import app_names
from tests.rules import Registry, Rule

#: One declared rule, as the three things this audit needs: what it is called, the node holding its detector's
#: body, and its module's string constants so a factored key can be resolved to its value.
ArmedRule = tuple[str, ast.AST, dict[str, str]]


def _is_rule_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Rule"


def _function_index(module: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda]:
    """Every local def by name, for resolving a ``find``/registered detector to its body."""
    return {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    }


def _string_constants(module: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments, by name."""
    constants: dict[str, str] = {}
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def _detector_body(
    find: ast.expr,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda],
) -> ast.AST:
    """The node whose literals are this rule's spelling.

    A bare name resolves to the function it names (``find=_foo`` alone says
    nothing about what ``_foo`` compares); a lambda is already the body."""
    return functions.get(find.id, find) if isinstance(find, ast.Name) else find


def declared_rules() -> Iterator[ArmedRule]:
    """Every ``Rule`` declared anywhere in the invariants suite.

    Derived from the tree rather than a module list: a left-out module is a
    rule nothing audits."""
    for path in sorted((REPO_ROOT / "tests" / "invariants").glob("test_*.py")):
        module = parsed(path)
        functions = _function_index(module)
        constants = _string_constants(module)
        for node in ast.walk(module):
            if not _is_rule_call(node) or not isinstance(node, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            find = keywords.get("find")
            name = getattr(keywords.get("name"), "value", "<unnamed rule>")
            if find is not None:
                yield (str(name), _detector_body(find, functions), constants)


#: A bare identifier, and one that is private.
_PRIVATE_IDENTIFIER = re.compile(r"_[A-Za-z][A-Za-z_0-9]*\Z")


def _private_keys(body: ast.AST, constants: dict[str, str]) -> set[str]:
    """The underscore-prefixed spellings this detector compares against.

    Only strings, only constants: a local named ``_seen`` is not a spelling the
    detector compares anything to."""
    keys: set[str] = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            keys.add(node.value)
        elif isinstance(node, ast.Name) and node.id in constants:
            keys.add(constants[node.id])
    return {key for key in keys if _PRIVATE_IDENTIFIER.match(key)}


def _hand_rolled_rule(entry: ArmedHandRolled) -> ArmedRule:
    """A registered hand-rolled detector as the (name, body, constants) the audit reads.
    The same shape ``declared_rules`` builds from a rule's ``find=``."""
    module = ast.parse(entry.module.read_text(encoding="utf-8"))
    body = _function_index(module).get(entry.function)
    if body is None:
        raise AssertionError(
            f"{entry.name}: {entry.module.name} defines no {entry.function!r}; "
            "a registered detector that no longer exists audits nothing"
        )
    return (entry.name, body, _string_constants(module))


def hand_rolled_armings() -> Iterator[ArmedRule]:
    """The audit's second half: detectors no ``Rule`` declares, as armed rules. Each detector module
    registers its observer in ``_OBSERVERS`` at import time -- import them here, before any premise runs,
    so a worker that hasn't collected the detector modules yet still sees registered observers.
    """
    import importlib

    for entry in hand_rolled_detectors():
        importlib.import_module(f"tests.invariants.{entry.module.stem}")
        entry.premise()
        yield _hand_rolled_rule(entry)


def missing_armings(
    rules: Sequence[ArmedRule] | None = None,
    hand_rolled: Sequence[ArmedRule] | None = None,
) -> list[str]:
    """Rules or hand-rolled detectors keyed on a private name the app tree never spells.
    Defaults sweep the whole inventory, so every detector is audited."""
    defined = app_names()
    found: list[str] = []
    for name, body, constants in rules if rules is not None else declared_rules():
        for key in sorted(_private_keys(body, constants)):
            if key not in defined:
                found.append(f"{name}: keyed on {key!r}, which no app module defines any more")
    for name, body, constants in hand_rolled if hand_rolled is not None else hand_rolled_armings():
        for key in sorted(_private_keys(body, constants)):
            if key not in defined:
                found.append(f"{name} (hand-rolled): keyed on {key!r}, which no app module defines any more")
    return found


def _detector(source: str) -> ast.AST:
    return ast.parse(source).body[0]


def test_a_detector_keyed_on_a_name_the_tree_lost_is_reported() -> None:
    """The bite: the audit has to fail on a name app/ does not define.

    A name absent from app/ IS the disarmed detector; planting one is the only
    way to see the audit reject it without breaking a real rule to find out."""
    assert missing_armings(
        [
            (
                "renamed-away-rule",
                _detector(
                    "def find(tree, path):\n"
                    "    return [f for f in walk() if f.attr == '_gone_from_the_app_tree']\n"
                ),
                {},
            )
        ]
    ) == ["renamed-away-rule: keyed on '_gone_from_the_app_tree', which no app module defines any more"]


def test_a_hand_rolled_detector_keyed_on_a_name_the_tree_lost_is_reported() -> None:
    """The H2 bite: the audit walks hand-rolled detectors, not only ``Rule`` finds.
    A detector outside every ``Rule(...)`` was invisible to this audit."""
    planted: list[ArmedRule] = [
        (
            "hand-rolled-gone",
            _detector(
                "def _find_offenders(tree, path):\n"
                "    return [f for f in walk() if f.attr == '_gone_from_the_app_tree']\n"
            ),
            {},
        )
    ]
    assert missing_armings(hand_rolled=planted) == [
        "hand-rolled-gone (hand-rolled): keyed on '_gone_from_the_app_tree', "
        "which no app module defines any more"
    ]


def test_a_key_factored_into_a_module_constant_is_audited_by_its_value() -> None:
    """The bite for the factored spelling: the constant's name is not the key.

    A rule holding ``_ACTION_CHAIN_KEY = "..."`` is armed on the value;
    auditing the variable would need a per-detector exemption."""
    body = _detector("def find(tree, path):\n    return [f for f in walk() if f.attr == _KEY]\n")
    assert not missing_armings([("factored-rule", body, {"_KEY": "run_action_chain"})])
    assert missing_armings([("factored-rule", body, {"_KEY": "_renamed_away"})]) == [
        "factored-rule: keyed on '_renamed_away', which no app module defines any more"
    ]


def test_a_detector_that_spares_private_names_is_not_reported() -> None:
    """The clean half: the audit is silent on a rule that keys on nothing private.

    An audit that reported every rule would pass its bite forever; ordinary
    vocabulary is not a key."""
    body = _detector("def find(tree, path):\n    return [f for f in walk() if f.attr in {'append', 'get'}]\n")
    assert not missing_armings([("vocabulary-rule", body, {})])


def test_a_hand_rolled_detector_that_spares_private_names_is_not_reported() -> None:
    """The clean half for hand-rolled detectors: ordinary vocabulary stays quiet."""
    planted: list[ArmedRule] = [
        ("hand-rolled-vocabulary", _detector("def f(tree, path):\n    return []\n"), {})
    ]
    assert not missing_armings(hand_rolled=planted)


def _rules() -> list[tuple[str, Rule]]:
    """Every rule the invariants suite declares, derived by import.

    Registries and standalone ``Rule`` constants both; enumeration mirrors ``declared_rules``."""
    found: dict[str, Rule] = {}
    for path in sorted((REPO_ROOT / "tests" / "invariants").glob("test_*.py")):
        module = importlib.import_module(f"tests.invariants.{path.stem}")
        for attr in sorted(vars(module)):
            value = getattr(module, attr)
            if isinstance(value, Registry):
                found.update((f"{path.stem}.{rule.name}", rule) for rule in value)
            elif isinstance(value, Rule):
                found[f"{path.stem}.{value.name}"] = value
    return sorted(found.items())


def test_ast_derivation_and_import_derivation_agree() -> None:
    """The two rule inventories are the same set of rules.

    If they diverge, one derivation misses rules the other sees.
    """
    ast_names = {name for name, _body, _constants in declared_rules()}
    import_names = {label.rsplit(".", 1)[1] for label, _rule in _rules()}
    assert ast_names == import_names, (
        f"AST-only rules: {sorted(ast_names - import_names)}; "
        f"import-only rules: {sorted(import_names - ast_names)}"
    )


def test_every_rule_bites() -> None:
    """Every rule's proof thunk executes and fires, naming its marker.

    The arming audit is a static spelling check; only running the proofs
    executes them -- this test is what a silent ``return []`` must get past."""
    rules = _rules()
    assert len(rules) >= 80, "the rule sweep found almost nothing"
    for label, rule in rules:
        try:
            rule.check_bites()
        except AssertionError as exc:
            raise AssertionError(f"[{label}] {exc}") from exc


def test_every_rule_is_still_armed_on_a_name_the_app_tree_has() -> None:
    """The sweep: not one detector (Rule-declared or hand-rolled) points at a gone name."""
    found = missing_armings()
    assert not found, (
        f"{len(found)} rule(s) key on a name the app tree no longer defines, so each matches "
        "nothing and reports green while governing nothing. Update the detector to the new "
        "spelling, or drop the rule if the property moved out of scope: " + "; ".join(found)
    )
