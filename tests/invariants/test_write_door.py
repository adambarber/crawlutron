"""Write-door invariant: every storage write funnels through txn_write/in_txn.

Decidable SQL shapes: literal operand, joined/synthetic operand, module-constant name. No module exempt."""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import REPO_ROOT, enclosing_function, offender_at, parent_map
from tests._walk_readers import call_nodes
from tests._walk_resolution import resolved_call_name
from tests.chokepoint_walks import trees_under
from tests.rules import Rule

_STORAGE_DIR = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "storage"

_WRITE_METHODS = frozenset({"execute", "executemany", "commit", "rollback", "executescript"})
_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "begin"})
_WRITE_DOOR = frozenset({"txn_write", "in_txn"})

#: Scope exclusions, path name -> justification.
#: ``sqlite_store.py``: connection factory, creates the connection the doors govern; predates any store.
_SCOPE_EXCLUDE: frozenset[str] = frozenset({"sqlite_store.py"})


def _is_conn_receiver(node: ast.AST) -> bool:
    """True when *node* is ``conn`` or any ``... ._conn``."""
    if isinstance(node, ast.Name) and node.id == "conn":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "_conn":
        return True
    return False


def _conn_method(call: ast.Call) -> ast.Attribute | None:
    """``call.func`` when its receiver ends in ``._conn`` or is bare ``conn``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _WRITE_METHODS:
        return None
    if _is_conn_receiver(func.value):
        return func
    return None


def _first_sql_string(call: ast.Call, tree: ast.Module | None = None) -> str | None:
    if not call.args:
        return None
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    if not isinstance(argument, ast.Name) or tree is None:
        return None
    # Look up the name in module-level string-constant assignments.
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != argument.id:
            continue
        val = node.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return val.value
    return None


def _is_write_statement(sql: str) -> bool:
    head = sql.lstrip().split()
    return bool(head) and head[0].lower() in _WRITE_KEYWORDS


def _inside_door(call: ast.Call, tree: ast.Module, parents: dict[int, ast.AST]) -> bool:
    """True when ``call`` is lexically inside a ``with txn_write``/``with in_txn`` block."""
    node: ast.AST | None = call
    while node is not None:
        parent = parents.get(id(node))
        if isinstance(parent, ast.With):
            for item in parent.items:
                context = item.context_expr
                if isinstance(context, ast.Call):
                    name = resolved_call_name(tree, context)
                    if name.rsplit(".", 1)[-1] in _WRITE_DOOR:
                        return True
        node = parent
    return False


def _inside_configure_store_connection(call: ast.Call, parents: dict[int, ast.AST]) -> bool:
    owner = enclosing_function(call, parents)
    return owner is not None and owner.name == "configure_store_connection"


def _bare_write_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Bare storage writes outside the write door, file and line named."""
    out: list[str] = []
    parents = parent_map(tree)
    for call in call_nodes(tree):
        func = _conn_method(call)
        if func is None:
            continue
        if func.attr == "cursor":
            continue
        if func.attr in {"commit", "rollback", "executescript"}:
            if not _inside_door(call, tree, parents) and not _inside_configure_store_connection(
                call, parents
            ):
                out.append(offender_at(path, call, f"direct {func.attr}() outside the write door"))
            continue
        sql = _first_sql_string(call, tree)
        if sql is None or not _is_write_statement(sql):
            continue
        if not _inside_door(call, tree, parents) and not _inside_configure_store_connection(call, parents):
            out.append(offender_at(path, call, "bare write statement outside the write door"))
    return out


WRITE_DOOR_RULE = Rule(
    name="storage-writes-funnel-through-the-write-door",
    find=_bare_write_offenders,
    summary=(
        "executes an INSERT/UPDATE/DELETE/BEGIN, or a direct commit/rollback/"
        "executescript, outside txn_write/in_txn/configure_store_connection; "
        "decidable SQL: literal, joined/synthetic, module-constant name"
    ),
    scope=lambda *, exclude: (
        (path, tree)
        for path, tree in trees_under(_STORAGE_DIR)
        if "migrations" not in path.parts and path.name not in _SCOPE_EXCLUDE
    ),
    bite=lambda: (
        "import crawlutron.storage.safe_db as safe_db\n\n"
        "safe_db.txn_write(conn)\n"
        'conn.execute("UPDATE t SET x = 1")\n',
        'alias._conn.execute("UPDATE t SET x = 1")\n',
        'SQL = "UPDATE t SET x = 1"\nconn.execute(SQL)\n',
        'store._conn.execute("BEGIN")\n',
    ),
    clean=lambda: (
        'with txn_write(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'with in_txn(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'def configure_store_connection(conn):\n    conn.execute("PRAGMA busy_timeout=5000")\n',
        'conn.execute("SELECT * FROM t")\n',
        'self._conn.execute("PRAGMA busy_timeout=5000")\n',
        'something._rows.execute("UPDATE t SET x = 1")\n',
    ),
)


def test_storage_writes_funnel_through_the_write_door() -> None:
    scope = WRITE_DOOR_RULE.scope
    if scope is None:
        raise AssertionError("write-door rule has no scope")
    offenders: list[str] = []
    for path, tree in scope(exclude=frozenset()):
        offenders.extend(_bare_write_offenders(tree, path))
    assert not offenders, "\n".join(offenders)


def test_write_door_rule_has_working_bite() -> None:
    WRITE_DOOR_RULE.check_bites()
