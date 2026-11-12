"""Every coordination_cookies read carries the namespace predicate. The defect foreclosed: ``all_slots`` ran
an unfiltered full-table SELECT, so every per-request cookie read deserialized every other tenant's rows
under the store lock.
"""

import ast
import pathlib

from tests.chokepoint_walks import APP_ROOT, parsed

_STORE = APP_ROOT / "storage" / "coordination_cookies.py"


def test_every_cookie_table_read_is_namespace_scoped() -> None:
    tree = parsed(_STORE)
    name = pathlib.Path(_STORE).name
    offenders = []
    for node in ast.walk(tree):
        if _is_unscoped_read(node):
            assert isinstance(node, ast.expr)
            offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"unfiltered coordination_cookies statement(s) reading payloads: {offenders}"


def _is_unscoped_read(node: ast.AST) -> bool:
    """A SQL string literal that SELECTs coordination_cookies columns but no
    namespace predicate, or a fetchall() over one -- the all_slots() shape."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    sql = node.value
    if "coordination_cookies" not in sql:
        return False
    if "SELECT" not in sql.upper():
        return False
    return "namespace = ?" not in sql
