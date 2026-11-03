"""The challenge-wait poll uses the injected clock and extracts at most twice.

``extract_with_challenge_wait`` must not re-extract the full DOM
(``page.content()``) on every 1 s tick, and must not sleep raw
(``asyncio.sleep``) so tests can inject the clock. One initial extract and one
final extract after the readyState poll; the poll itself uses
``self._async_sleep(1)`` (the injected clock).
"""

import ast

from tests.chokepoint_walks import REPO_ROOT, parsed, stmt_offenders

_BROWSER = REPO_ROOT / "crawlutron" / "transport" / "browser.py"


def test_challenge_wait_injected_clock_two_extracts() -> None:
    tree = parsed(_BROWSER)
    offenders: list[str] = []
    offenders.extend(_raw_sleep_offenders(tree))
    offenders.extend(_too_many_extract_offenders(tree))
    assert not offenders, f"browser.py: {offenders}"


def _call_of(stmt: ast.AST) -> ast.Call | None:
    """The call behind an expression statement, unwrapping ``await``."""
    value = stmt.value if isinstance(stmt, ast.Expr) else None
    if isinstance(value, ast.Await):
        value = value.value
    return value if isinstance(value, ast.Call) else None


def _challenge_wait_target(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "extract_with_challenge_wait"
        ):
            return node
    return None


def _is_raw_asyncio_sleep_stmt(stmt: ast.AST) -> bool:
    call = _call_of(stmt)
    return bool(
        call
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "sleep"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
    )


def _raw_sleep_offenders(tree: ast.Module) -> list[str]:
    target = _challenge_wait_target(tree)
    return stmt_offenders(
        tree,
        _BROWSER,
        lambda node: node is target,
        _is_raw_asyncio_sleep_stmt,
        "raw asyncio.sleep in the challenge-wait loop; use self._async_sleep",
    )


def _is_extract_stmt(stmt: ast.AST) -> bool:
    call = _call_of(stmt)
    return (
        call is not None
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in {"extract_html", "extract_text"}
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    )


def _too_many_extract_offenders(tree: ast.Module) -> list[str]:
    target = _challenge_wait_target(tree)
    if target is None:
        return []
    calls = 0
    for stmt in ast.walk(target):
        if _is_extract_stmt(stmt):
            calls += 1
    if calls > 4:
        return [
            f"{_BROWSER.name}:{target.lineno} extract_with_challenge_wait calls "
            f"extract_html/extract_text {calls} times; cap at 4"
        ]
    return []
