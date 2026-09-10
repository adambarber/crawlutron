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

from chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    STRATEGIES_DIR,
    app_python_files,
    app_trees,
    file_tree,
    offender,
    trees_under,
)

__all__ = [
    "REPO_ROOT",
    "SANCTIONED",
    "STRATEGIES_DIR",
    "app_python_files",
    "app_trees",
    "file_tree",
    "offender",
    "trees_under",
]

# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from
# attacker-influenced text (the ReDoS shape). `re.search(LITERAL, text)` is
# safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = {"compile", "search", "match", "fullmatch", "sub", "findall", "split"}


def _dynamic_pattern_re_calls(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag re.<method>(<non-literal pattern>, ...) -- the ReDoS shape."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in _RE_DYNAMIC_METHODS
            and node.args
            and not isinstance(node.args[0], ast.Constant)
        ):
            offenders.append(offender(path, node.lineno, f"re.{node.func.attr}(<dynamic pattern>)"))
    return offenders


def test_no_dynamic_pattern_re_calls():
    """re.* with a non-literal pattern must go through app.safe_regex."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_dynamic_pattern_re_calls(tree, path))
    assert not offenders, "Use app.safe_regex.compile_untrusted/match_bounded: " + ", ".join(offenders)


def test_no_direct_regex_engine_import():
    """Only safe_regex.py may import the third-party `regex` engine."""
    offenders: list[str] = []
    for path, tree in app_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "regex":
                        offenders.append(offender(path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "regex":
                    offenders.append(offender(path, node.lineno))
    assert not offenders, "Only app.safe_regex may import the regex engine: " + ", ".join(offenders)


def test_no_bare_re_compile_on_nonliteral():
    """Non-literal re.compile must go through app.safe_regex.compile_untrusted."""
    offenders: list[str] = []
    for path, tree in app_trees():
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
                offenders.append(offender(path, node.lineno))
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
                    offenders.append(offender(path, inner.lineno))
            break
    return offenders


def test_no_await_inside_lock_bodies():
    """A lock held across an await blocks the loop and can deadlock (M3)."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_lock_bodies_with_awaits(tree, path))
    assert not offenders, "Lock held across await: " + ", ".join(offenders)


# --- SafeUrl chokepoint (final review) ------------------------------------

# Files migrated onto app.safe_url must not regress to hand-rolled redirect
# hops: urljoin-based hop construction outside safe_url.py reintroduces the
# "forgot one of the five steps" class. (The http fetcher's own hop path
# and link/robots/sitemap URL *construction* are separate uses, not hops.)
_SAFE_URL_MIGRATED = {
    "app/crawl/strategies/document.py",
    "app/api/webhooks.py",
}


def test_no_hand_rolled_redirect_hops_in_migrated_files():
    """Redirect hops in migrated files go through SafeUrl.redirect_hop."""
    offenders: list[str] = []
    for rel in _SAFE_URL_MIGRATED:
        path, tree = file_tree(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin":
                offenders.append(offender(path, node.lineno))
    assert not offenders, "Use SafeUrl.redirect_hop for redirect hops: " + ", ".join(offenders)


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
            offenders.append(offender(path, node.lineno, ".read()"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "content"
            and isinstance(node.value, ast.Name)
            and node.value.id.lower().startswith("response")
        ):
            offenders.append(offender(path, node.lineno, ".content"))
    return offenders


def test_no_bare_body_reads_in_strategies():
    """Every body read in strategies/ must be capped (N2 class).

    Scope note: the chokepoint (app/bounded_io.py) lives OUTSIDE strategies/
    so its own ``stream.read(n)`` calls are exempt by path; the other
    body-acquisition path (app/search/providers.py) routes through
    read_capped_async. Widening this scope must keep those exemptions.
    """
    offenders: list[str] = []
    for path, tree in trees_under(STRATEGIES_DIR):
        offenders.extend(_bare_body_reads(tree, path))
    assert not offenders, "Use app.bounded_io.read_capped_sync/read_capped_async: " + ", ".join(offenders)


def _scope_matcher_definitions() -> list[str]:
    """File labels of every ``def scope_allows``/``_scope_allows`` under app/.

    M10: the bounded scope check was verbatim-copied into engine.py and
    recursive.py; a one-sided fix would have left the other on the stale
    contract. Deduped into app/crawl/scope_match.py -- a second definition
    anywhere means the copy crept back.
    """
    defs: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "scope_allows",
                "_scope_allows",
            }:
                defs.append(offender(path, node.lineno))
    return defs


def test_scope_matcher_has_exactly_one_definition():
    """The fail-closed scope matcher must live in one module only.

    Behavior parity across the dedupe is covered by test_scope_regex.py;
    this pins the structural property that no second copy exists to drift.
    """
    defs = _scope_matcher_definitions()
    assert len(defs) == 1, f"scope matcher duplicated again: {defs}"
    assert "app/crawl/scope_match.py" in defs[0], defs
