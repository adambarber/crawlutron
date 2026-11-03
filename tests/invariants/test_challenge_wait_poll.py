"""The challenge-wait poll uses the injected clock and extracts at most twice.

``extract_with_challenge_wait`` must not re-extract the full DOM
(``page.content()``) on every 1 s tick, and must not sleep raw
(``asyncio.sleep``) so tests can inject the clock. One initial extract and one
final extract after the readyState poll; the poll itself uses
``self._async_sleep(1)`` (the injected clock).
"""

import ast

from tests._walk_assertions import assert_detectors_clean
from tests.chokepoint_walks import REPO_ROOT, attribute_call_on, offender_at, parsed

_BROWSER = REPO_ROOT / "crawlutron" / "transport" / "browser.py"


def test_challenge_wait_injected_clock_two_extracts() -> None:
    assert_detectors_clean(
        parsed(_BROWSER),
        path=_BROWSER,
        detectors=(_raw_sleep_offenders, _too_many_extract_offenders),
    )


def _challenge_wait_target(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "extract_with_challenge_wait"
        ):
            return node
    return None


def _is_asyncio_sleep(call: ast.Call) -> bool:
    return attribute_call_on(call, "asyncio", "sleep")


def _is_extract_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in {"extract_html", "extract_text"}
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    )


def _calls_in_target(tree: ast.Module, predicate) -> list[ast.Call]:
    """Every ``ast.Call`` inside ``extract_with_challenge_wait`` matching
    ``predicate``, regardless of statement shape (``ast.Expr``, ``ast.Assign``,
    ...)."""
    target = _challenge_wait_target(tree)
    if target is None:
        return []
    return [n for n in ast.walk(target) if isinstance(n, ast.Call) and predicate(n)]


def _raw_sleep_offenders(tree: ast.Module) -> list[str]:
    return [
        offender_at(_BROWSER, call, "raw asyncio.sleep in the challenge-wait loop; use self._async_sleep")
        for call in _calls_in_target(tree, _is_asyncio_sleep)
    ]


def _too_many_extract_offenders(tree: ast.Module) -> list[str]:
    calls = len(_calls_in_target(tree, _is_extract_call))
    if calls <= 4:
        return []
    target = _challenge_wait_target(tree)
    return [
        f"{_BROWSER.name}:{target.lineno} extract_with_challenge_wait calls "
        f"extract_html/extract_text {calls} times; cap at 4"
    ]
