"""Executable safety invariants: recurring bug classes blocked at the door.

The round-4 review's central lesson (validated empirically): static
rejection of "pathological" shapes is bypassable, and chokepoints only
hold if bypassing them fails CI instead of code review. These AST walks
verify the UNSAFE patterns are absent from app/ so reintroducing them
turns the build red.
"""

from __future__ import annotations

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# app/safe_regex.py is the sanctioned wrapper around engine-level compilation.
_CHOKEPOINT_MODULES = {"safe_regex.py"}


def _app_python_files():
    return sorted(p for p in APP_ROOT.rglob("*.py") if p.name not in _CHOKEPOINT_MODULES)


def test_no_bare_re_compile_on_nonliteral():
    """Non-literal re.compile must go through app.safe_regex.compile_untrusted."""
    offenders: list[str] = []
    for path in _app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compile"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.args
                and not isinstance(node.args[0], ast.Constant)
            ):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno}")
    assert not offenders, "Use app.safe_regex.compile_untrusted for non-literal patterns: " + ", ".join(
        offenders
    )


def _lock_bodies_with_awaits(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag `with <lock>:` bodies containing an await (the M3 class).

    A THREADING lock held across an await blocks the event loop and can
    deadlock. `async with` (asyncio.Lock/Condition) bodies may legitimately
    await -- that is the sanctioned pattern -- so only sync `with` is
    checked. Sync-with bodies that await include any `threading.Lock`/
    `RLock` context (which is what ContentCache.aset did before its N1
    rework).
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if isinstance(node, ast.AsyncWith):
            continue  # asyncio.Lock bodies may await by design
        for item in node.items:
            # `with self._lock:` / `with lock:` / `with some_lock:` --
            # a sync lock context. Any await inside is a loop-blocker.
            expr = item.context_expr
            looks_like_lock = (isinstance(expr, ast.Attribute) and expr.attr.endswith("lock")) or (
                isinstance(expr, ast.Name) and "lock" in expr.id
            )
            if not looks_like_lock:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Await):
                    offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{inner.lineno}")
            break
    return offenders


def test_no_await_inside_lock_bodies():
    """A lock held across an await blocks the loop and can deadlock (M3)."""
    offenders: list[str] = []
    for path in _app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_lock_bodies_with_awaits(tree, path))
    assert not offenders, "Lock held across await: " + ", ".join(offenders)


# --- bounded I/O (chokepoints review) -------------------------------------


def _bare_body_reads(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag bare `.read()` calls and `.content` attribute access.

    Both acquire an unbounded body. Capped reads go through
    app/bounded_io.read_capped_* ; curl_cffi's `.body` attribute and
    kernel/sysfs reads outside strategies/ are not body acquisitions.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read"
            and not node.args
            and not node.keywords
        ):
            offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno} .read()")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "content"
            and isinstance(node.value, ast.Name)
            and node.value.id.lower().startswith("response")
        ):
            offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.lineno} .content")
    return offenders


def test_no_bare_body_reads_in_strategies():
    """Every body read in strategies/ must be capped (N2 class)."""
    strategies = APP_ROOT / "crawl" / "strategies"
    offenders: list[str] = []
    for path in sorted(strategies.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_bare_body_reads(tree, path))
    assert not offenders, "Use app.bounded_io.read_capped_sync/read_capped_async: " + ", ".join(offenders)
