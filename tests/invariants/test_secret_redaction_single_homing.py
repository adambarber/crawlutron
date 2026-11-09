"""Invariant: every secret-redaction implementation lives in one module (#660).

Direction analysis: this gate seals the dangerous direction only — masking
implementation appearing OUTSIDE the home. Adding pure delegation
(call-through into url_redaction) anywhere stays legal, so growth of the
caller population cannot red; the only red is a second implementation.
"""

import ast
from pathlib import Path

from tests.chokepoint_walks import REPO_ROOT

#: The one sanctioned home for masking implementation.
HOME = Path("crawlutron/safety/url_redaction.py")

#: String-literal markers that betray a masking implementation.
MASKING_MARKERS = ("[REDACTED]", "[unparseable-webhook-url]")

#: A query-secret regex span shape, e.g. the [?&]name=value matcher.
_PARAM_SPAN_TOKEN = "[?&]"

#: Name fragments that make a function redaction surface.
_NAME_PARTS = ("redact", "mask", "scrub")


def _is_redaction_named(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _NAME_PARTS)


def _masking_violations(path: Path, tree: ast.AST) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(marker in node.value for marker in MASKING_MARKERS):
                findings.append(f"{path}:{node.lineno} masking marker literal {node.value!r}")
            if _PARAM_SPAN_TOKEN in node.value and "=" in node.value:
                findings.append(f"{path}:{node.lineno} secret-param span pattern {node.value!r}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_redaction_named(node.name):
            if _has_own_masking_logic(node):
                findings.append(f"{path}:{node.lineno} {node.name}() carries its own masking logic")
    return findings


def _has_own_masking_logic(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body masks itself: a branch or literal, and no home call."""
    calls_home = any(
        "url_redaction" in ast.unparse(stmt) for stmt in ast.walk(ast.Module(body=func.body, type_ignores=[]))
    )
    branches = [
        node
        for stmt in func.body
        for node in ast.walk(stmt)
        if isinstance(node, ast.If) and node.col_offset > func.col_offset
    ]
    return bool(branches) and not calls_home


def test_secret_redaction_is_single_homed_in_url_redaction():
    for path in sorted((REPO_ROOT / "crawlutron").rglob("*.py")):
        if path == REPO_ROOT / HOME:
            continue
        tree = ast.parse(path.read_text())
        for finding in _masking_violations(path.relative_to(REPO_ROOT), tree):
            raise AssertionError(f"{finding} -- secret redaction is single-homed in {HOME}")
