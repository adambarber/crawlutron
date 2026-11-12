"""Invariant: every secret-redaction implementation lives in one module (#660). Direction analysis: the gate
seals the dangerous direction only -- masking implementation appearing OUTSIDE the home; pure delegation
stays legal, so caller growth cannot red -- the only red is a second implementation.
"""

import ast
from pathlib import Path

from tests._walk_primitives import string_constants
from tests.chokepoint_walks import APP_ROOT, REPO_ROOT

#: The one sanctioned home for masking implementation.
HOME = Path("packages/core/crawlutron/safety/url_redaction.py")

#: String-literal markers that betray a masking implementation.
MASKING_MARKERS = ("[REDACTED]", "[unparseable-webhook-url]")

#: A query-secret regex span shape, e.g. the [?&]name=value matcher.
_PARAM_SPAN_TOKEN = "[?&]"

#: Mask abbreviations a branchless implementation might reach for.
_MASK_ABBREVIATIONS = ("***", "…")

# Known blind shape (documented boundary, not closed): a marker built by
# string concatenation ("REDA" + "CTED") is invisible to ast.Constant.
_SPLIT_BOUNDS = ("?", "&", ";", "#")  # boundaries a branchless splitter cuts on
#: Name fragments that make a function redaction surface.
_NAME_PARTS = ("redact", "mask", "scrub")


def _is_redaction_named(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _NAME_PARTS)


def _masking_violations(path: Path, tree: ast.AST) -> list[str]:
    findings: list[str] = []
    for lineno, value in string_constants(tree):
        if any(marker in value for marker in MASKING_MARKERS):
            findings.append(f"{path}:{lineno} masking marker literal {value!r}")
        if _PARAM_SPAN_TOKEN in value and "=" in value:
            findings.append(f"{path}:{lineno} secret-param span pattern {value!r}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_redaction_named(node.name):
            if _has_own_masking_logic(node):
                findings.append(f"{path}:{node.lineno} {node.name}() carries its own masking logic")
    return findings


def _has_own_masking_logic(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body masks itself: a branch, or a split-rebuild with mask abbreviation, no home call."""
    calls_home = any(
        "url_redaction" in ast.unparse(stmt) for stmt in ast.walk(ast.Module(body=func.body, type_ignores=[]))
    )
    if calls_home:
        return False
    branches = [
        node
        for stmt in func.body
        for node in ast.walk(stmt)
        if isinstance(node, ast.If) and node.col_offset > func.col_offset
    ]
    return bool(branches) or _split_rebuild_mask(func)


def _split_rebuild_mask(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Branchless masking: splits a query/URL boundary and joins a mask abbreviation."""
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    splits = literals = False
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "split"
            and any(isinstance(a, ast.Constant) and a.value in _SPLIT_BOUNDS for a in node.args)
        ):
            splits = True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals = literals or any(abbr in node.value for abbr in _MASK_ABBREVIATIONS)
    return splits and literals


def test_secret_redaction_is_single_homed_in_url_redaction():
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path == REPO_ROOT / HOME:
            continue
        tree = ast.parse(path.read_text())
        for finding in _masking_violations(path.relative_to(REPO_ROOT), tree):
            raise AssertionError(f"{finding} -- secret redaction is single-homed in {HOME}")


def test_a_branchless_split_rebuild_mask_reds():
    src = 'def mask_url(u):\n    return u.split("?")[0] + "***"\n'
    violations = _masking_violations(Path("probe.py"), ast.parse(src))
    assert violations, "branchless split-rebuild mask must red"
    assert any("mask_url" in v for v in violations)


def test_a_branchless_delegator_stays_green():
    src = "def mask_url(u):\n    return redact_url(u)\n"
    assert _masking_violations(Path("probe.py"), ast.parse(src)) == []
