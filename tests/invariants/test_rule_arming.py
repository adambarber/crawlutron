"""Is every rule still ARMED on a name the app tree actually has?

Move a literal name and the comparisons stop matching -- the rule reports green
forever; bite proofs are synthetic, so the ``find=`` underscore keys are audited."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence

from tests.chokepoint_walks import REPO_ROOT
from tests.invariants._shared import app_names

#: One declared rule, as the three things this audit needs: what it is called, the node holding its detector's
#: body, and its module's string constants so a factored key can be resolved to its value.
ArmedRule = tuple[str, ast.AST, dict[str, str]]


def _is_rule_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Rule"


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


def _detector_body(find: ast.expr, functions: dict[str, ast.AST]) -> ast.AST:
    """The node whose literals are this rule's spelling.

    A bare name resolves to the function it names (``find=_foo`` alone says
    nothing about what ``_foo`` compares); a lambda is already the body."""
    return functions.get(find.id, find) if isinstance(find, ast.Name) else find


def declared_rules() -> Iterator[ArmedRule]:
    """Every ``Rule`` declared anywhere in the invariants suite.

    Derived from the tree rather than a module list: a left-out module is a
    rule nothing audits."""
    for path in sorted((REPO_ROOT / "tests" / "invariants").glob("test_*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        }
        constants = _string_constants(module)
        for node in ast.walk(module):
            if not _is_rule_call(node):
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


def missing_armings(rules: Sequence[ArmedRule] | None = None) -> list[str]:
    """Rules keyed on a private name the app tree does not spell."""
    defined = app_names()
    found: list[str] = []
    for name, body, constants in rules if rules is not None else declared_rules():
        for key in sorted(_private_keys(body, constants)):
            if key not in defined:
                found.append(f"{name}: keyed on {key!r}, which no app module defines any more")
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


def test_every_rule_is_still_armed_on_a_name_the_app_tree_has() -> None:
    """The sweep: not one of the suite's detectors points at a name that is gone."""
    found = missing_armings()
    assert not found, (
        f"{len(found)} rule(s) key on a name the app tree no longer defines, so each matches "
        "nothing and reports green while governing nothing. Update the detector to the new "
        "spelling, or drop the rule if the property moved out of scope: " + "; ".join(found)
    )
