"""Is every rule still ARMED on a name the app tree actually has?

A detector is a set of comparisons against literal names. Move a name in the app
tree and the comparisons keep comparing -- they simply stop matching anything,
because the thing they were written to police is spelled differently now. The
rule then reports green forever. This is not a hypothesis: two rules in this
suite went that way when the browser split moved `_release_browser` and
`_run_action_chain` into a new module, and nothing noticed for a commit.

The reason `check_bites` could not see it is worth stating, because it is why
this file exists rather than a fix inside `check_bites`. A bite proof plants a
synthetic source the rule itself declares, so it proves the detector *can*
fire. It cannot prove the detector is still *aimed* at the tree, because the
proof source is written in the test and the app tree is never consulted. Both
halves can be perfectly green while the rule governs nothing.

Why underscore-prefixed identifiers, and not every string in a detector. A
detector's literals are mostly ordinary vocabulary -- `append`, `status`, `get`,
`id` -- and demanding those resolve to an app definition would either be wrong
or, worse, satisfiable by noise. An underscore-prefixed name is a different
kind of token: nothing writes `_persist` by accident, so a detector naming one
has chosen the symbol it polices, chosen it over the public spelling, and said
so in the only vocabulary available to it. This suite is currently moving the
other way -- #94 and the BrowserDriver split are promotions of private names to
public -- which makes "the private spelling stopped existing" the most likely
way a rule here is silently disarmed.

Two precisions that keep the sweep honest rather than merely strict. A detector
that factors its key into a module constant is followed to the constant's
*value*, because the spelling that matters is the value: a rule holding
`_ACTION_CHAIN_KEY = "..."` is audited on what it says, not on the test-side
name of the variable holding it. And only the ``find=`` body is audited, not
the rule's proof sources -- a proof is a synthetic snippet by definition, so
its private names say nothing about what the rule polices in production, and
counting them would report every rule in the suite as disarmed.

What this does not cover, stated rather than left to be discovered: a detector
armed on a *public* app name, which has no private spelling to go red on. The
framework's own answer for that case is a `premise` -- "() -> None asserting the
rule's trigger set is non-empty" -- and the one rule in this suite whose key is
public carries one.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence

from tests.chokepoint_walks import REPO_ROOT
from tests.invariants._shared import app_names

#: One declared rule, as the three things this audit needs: what it is called,
#: the node holding its detector's body, and its module's string constants so a
#: factored key can be resolved to its value.
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

    A bare name is resolved to the function it names, because ``find=_foo`` on
    its own says nothing about what ``_foo`` compares against -- and resolving
    it is also what keeps the detector's own test-side name out of the audit.
    A lambda is already the body.
    """
    return functions.get(find.id, find) if isinstance(find, ast.Name) else find


def declared_rules() -> Iterator[ArmedRule]:
    """Every ``Rule`` declared anywhere in the invariants suite.

    Derived from the tree rather than from a list of modules, because a module
    that declared a rule and was left out of the list is a rule nothing audits
    -- the same hand-enumerated coverage the live-doc reference gate was rebuilt
    to remove, and the same reason the two gates that went stale after the
    browser split were not caught by anything else.
    """
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


#: A bare identifier, and one that is private. The shape is the whole point:
#: a detector keys on spellings, so `"_store_set_cookies() outside "` (a
#: sentence in a failure message that happens to mention one) and `"_"` (an
#: underscore count in `startswith`) are both strings that are not keys. Both
#: were reported by the first cut of this audit, which is the whole argument for
#: checking the shape rather than the prefix.
_PRIVATE_IDENTIFIER = re.compile(r"_[A-Za-z][A-Za-z_0-9]*\Z")


def _private_keys(body: ast.AST, constants: dict[str, str]) -> set[str]:
    """The underscore-prefixed spellings this detector compares against.

    Only strings, and only constants: a local variable named ``_seen`` is not a
    spelling the detector compares anything to, and treating its own name as a
    key would report a detector for a private name it never mentions as a
    spelling.
    """
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

    Without this the sweep is a plausible-looking assertion that a broken walk
    would satisfy forever -- the very failure this module documents, restated
    as its own proof. A name absent from app/ *is* the disarmed detector, and
    planting it is the only way to see the audit reject one without breaking a
    real rule to find out.
    """
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

    A rule holding `_ACTION_CHAIN_KEY = "..."` is armed on what the constant
    says. Auditing the test-side variable name instead would report the rule as
    keyed on a private name that no app module defines -- true of the variable,
    irrelevant to the rule -- and the audit would then need an exemption per
    detector, which is how a gate stops meaning anything.
    """
    body = _detector("def find(tree, path):\n    return [f for f in walk() if f.attr == _KEY]\n")
    assert not missing_armings([("factored-rule", body, {"_KEY": "run_action_chain"})])
    assert missing_armings([("factored-rule", body, {"_KEY": "_renamed_away"})]) == [
        "factored-rule: keyed on '_renamed_away', which no app module defines any more"
    ]


def test_a_detector_that_spares_private_names_is_not_reported() -> None:
    """The clean half: the audit is silent on a rule that keys on nothing private.

    An audit that reported every rule would pass its bite forever and be
    worthless, so this pins the other direction -- ordinary vocabulary in a
    detector is not a key, and a detector with no private spelling at all is
    left alone.
    """
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
