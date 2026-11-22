"""Contract: dependency-review base-ref must not resolve to the null SHA on tag pushes."""

from __future__ import annotations

import ast
from collections.abc import Mapping

import pytest

from tests._ci_contract import WORKFLOW_PATH, workflow_document

ZERO_SHA = "0" * 40
DEPENDENCY_REVIEW_JOB = "dependency-review"
DEPENDENCY_REVIEW_STEP = "Dependency review"
BASE_REF_INPUT = "base-ref"

TAG_PUSH: dict[str, object] = {"event": {"before": ZERO_SHA}, "sha": "a" * 40}
PR_PUSH: dict[str, object] = {
    "event": {"before": "b" * 40, "pull_request": {"base": {"sha": "c" * 40}}},
    "sha": "d" * 40,
}

_UNGUARDED_EXPRESSION = "${{ github.event.before || github.event.pull_request.base.sha }}"


def _base_ref_expression() -> str:
    """The base-ref expression the dependency-review job actually declares."""
    document = workflow_document(WORKFLOW_PATH)
    jobs = document["jobs"]
    assert isinstance(jobs, dict), "ci.yml must declare jobs"
    job = jobs.get(DEPENDENCY_REVIEW_JOB)
    assert isinstance(job, dict), f"ci.yml must declare the {DEPENDENCY_REVIEW_JOB} job"
    steps = job.get("steps")
    assert isinstance(steps, list), f"the {DEPENDENCY_REVIEW_JOB} job must declare steps"
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("name") != DEPENDENCY_REVIEW_STEP:
            continue
        with_block = step.get("with")
        assert isinstance(with_block, dict), "the Dependency review step must declare with"
        assert BASE_REF_INPUT in with_block, "the Dependency review step must declare base-ref"
        return str(with_block[BASE_REF_INPUT])
    raise AssertionError("the Dependency review step declares no base-ref input")


def _path(node: ast.Attribute) -> str:
    """Flatten ``github.event.before``-style attribute chains to dotted paths."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    assert isinstance(current, ast.Name) and current.id == "github", (
        f"unsupported expression root: {ast.dump(node)!r}"
    )
    return ".".join(reversed(parts))


def _lookup(dotted: str, payload: Mapping[str, object]) -> str:
    """Resolve a dotted payload path; a missing path is the empty string (falsy)."""
    node: object = payload
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part, "")
    return node if isinstance(node, str) else ""


def _evaluate(expression: str, payload: Mapping[str, object]) -> str:
    """Evaluate a base-ref expression body (short-circuit, constants, github lookups)."""
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    # GitHub Actions || / && are not Python operators; normalize them before
    # parsing so the AST visitor can evaluate short-circuit semantics.
    body = body.replace("||", " or ").replace("&&", " and ")
    tree = ast.parse(body, mode="eval")

    def _eval(node: ast.AST) -> object:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BoolOp):
            values = [_eval(v) for v in node.values]
            if isinstance(node.op, ast.Or):
                for value in values:
                    if value:
                        return value
                return values[-1] if values else ""
            if isinstance(node.op, ast.And):
                result = values[0] if values else ""
                for value in values[1:]:
                    if not result:
                        return result
                    result = value
                return result
            raise AssertionError(f"unsupported bool op: {ast.dump(node)!r}")
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comparator in zip(node.ops, node.comparators, strict=False):
                right = _eval(comparator)
                if isinstance(op, ast.NotEq):
                    if left != right:
                        return True
                elif isinstance(op, ast.Eq):
                    if left == right:
                        return True
                else:
                    raise AssertionError(f"unsupported compare op: {ast.dump(op)!r}")
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Attribute):
            return _lookup(_path(node), payload)
        raise AssertionError(f"unsupported expression node: {ast.dump(node)!r}")

    result = _eval(tree.body)
    return result if isinstance(result, str) else str(result)


def _is_real_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value) and value != ZERO_SHA


def test_base_ref_guard_keeps_release_coverage_on_tag_push() -> None:
    """On a tag push the resolved base-ref must be a real commit, never the null SHA."""
    expression = _base_ref_expression()
    resolved = _evaluate(expression, TAG_PUSH)
    assert resolved != ZERO_SHA, f"tag push resolved base-ref to the null SHA: {expression!r}"
    assert _is_real_sha(resolved), f"tag push resolved base-ref is not a real commit: {resolved!r}"


def test_base_ref_guard_is_load_bearing() -> None:
    """Bite: removing the guard lets a tag push resolve to the null SHA."""
    resolved = _evaluate(_UNGUARDED_EXPRESSION, TAG_PUSH)
    assert resolved == ZERO_SHA, (
        f"unguarded expression should reproduce the null-SHA defect, got {resolved!r}"
    )


def test_base_ref_no_pr_regression() -> None:
    """On a pull request the resolved base-ref must stay the push before value."""
    expression = _base_ref_expression()
    resolved = _evaluate(expression, PR_PUSH)
    assert resolved == "b" * 40, f"PR push base-ref regressed: {resolved!r}"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
