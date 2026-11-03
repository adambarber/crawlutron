"""Every coordination_cookies read carries the namespace predicate.

The defect this forecloses: ``all_slots()`` ran an unfiltered full-table
SELECT, so every per-request cookie read deserialized every other tenant's
rows under the store lock. Namespace scoping lives on the SQL statement, and
this gate refuses any statement that reads or deserializes payloads without
it. ``clear()``'s full-table DELETE is the one named exception: it removes
rows without reading or deserializing them.
"""

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, parsed

_STORE = REPO_ROOT / "crawlutron" / "storage" / "coordination_cookies.py"


def test_every_cookie_table_read_is_namespace_scoped() -> None:
    tree = parsed(_STORE)
    name = pathlib.Path(_STORE).name
    offenders = [f"{name}:{node.lineno}" for node in ast.walk(tree) if _is_unscoped_read(node)]
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
