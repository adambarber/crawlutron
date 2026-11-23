"""AST-derived facts for the two scripts whose structure is a CI contract."""

from __future__ import annotations

import ast
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tests._ci_contract import SERVICE_ROOT
from tests._walk_primitives import canonical_tree

STRICT = ConfigDict(extra="forbid", frozen=True)


class ScriptFact(BaseModel):
    """One typed AST observation: its kind, name, value and source line."""

    model_config = STRICT
    kind: str
    name: str
    value: str = ""
    line: int


class ScriptFacts(BaseModel):
    model_config = STRICT
    path: Path
    facts: tuple[ScriptFact, ...]

    def of_kind(self, kind: str) -> tuple[ScriptFact, ...]:
        return tuple(fact for fact in self.facts if fact.kind == kind)

    def has_string(self, value: str) -> bool:
        return any(fact.kind == "string" and fact.value == value for fact in self.facts)

    def has_compare(self, left: str, op: str, right: str) -> bool:
        name = f"{left} {op} {right}"
        return any(fact.kind == "compare" and fact.name == name for fact in self.facts)


def parse_script(path: Path) -> ScriptFacts:
    """Parse once through the project AST chokepoint; rules read typed facts only."""
    tree = canonical_tree(path)
    return ScriptFacts(path=path, facts=tuple(_facts(tree)))


def _facts(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield ScriptFact(kind="string", name="literal", value=node.value, line=node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ScriptFact(kind="function", name=node.name, line=node.lineno)
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name:
                yield ScriptFact(kind="call", name=name, line=node.lineno)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left = _name(node.left)
            right = _name(node.comparators[0])
            if left and right:
                op = _operator(node.ops[0])
                yield ScriptFact(kind="compare", name=f"{left} {op} {right}", line=node.lineno)
        elif isinstance(node, ast.Name) and node.id == "PYTHONDONTWRITEBYTECODE":
            yield ScriptFact(kind="env-key", name=node.id, line=node.lineno)


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _operator(node: ast.cmpop) -> str:
    return {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.In: "in",
        ast.NotIn: "not in",
        ast.Is: "is",
        ast.IsNot: "is not",
    }.get(type(node), "?")


CONFTEST = parse_script(SERVICE_ROOT / "tests" / "conftest.py")
IMAGE_SCRIPT = parse_script(SERVICE_ROOT / "scripts" / "release" / "image.py")
