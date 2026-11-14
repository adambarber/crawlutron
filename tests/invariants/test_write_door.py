"""Write-door invariant: every storage write funnels through txn_write/in_txn.

Storage modules not yet migrated are exempt and walked but not judged.
"""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import REPO_ROOT, enclosing_function, offender_at, parent_map
from tests._walk_readers import call_nodes
from tests._walk_resolution import resolved_call_name
from tests.chokepoint_walks import trees_under
from tests.rules import Rule

_STORAGE_DIR = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "storage"

#: Storage modules not yet migrated to txn_write/in_txn; each names why it is still allowed bare writes.
#: The set shrinks one store per arc until the gate seals at zero; the throttle store is the mutation target.
_EXEMPT_MODULES = frozenset(
    {
        "packages/core/src/crawlutron/storage/admin_audit_store.py": "awaiting the admin-audit arc",
        "packages/core/src/crawlutron/storage/caller_key_store.py": "awaiting the caller-key arc",
        "packages/core/src/crawlutron/storage/coordination_cookies.py": "awaiting the cookies arc",
        "packages/core/src/crawlutron/storage/coordination_sync_slots.py": "awaiting the sync-slots arc",
        "packages/core/src/crawlutron/storage/job_store.py": "awaiting the job-store arc",
        "packages/core/src/crawlutron/storage/job_store_accumulators.py": "awaiting the accumulators arc",
        "packages/core/src/crawlutron/storage/job_store_lease.py": "awaiting the job-store arc",
        "packages/core/src/crawlutron/storage/result_store.py": "awaiting the result-store arc",
        "packages/core/src/crawlutron/storage/watch_store.py": "awaiting the watch-store arc",
    }
)

_WRITE_METHODS = frozenset({"execute", "executemany", "commit", "rollback", "executescript"})
_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "begin"})
_WRITE_DOOR = frozenset({"txn_write", "in_txn"})


def _conn_method(call: ast.Call) -> ast.Attribute | None:
    """``call.func`` when it is ``self._conn.<write-method>``/``conn.<write-method>``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _WRITE_METHODS:
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "conn":
        return func
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "self"
        and receiver.attr == "_conn"
    ):
        return func
    return None


def _first_sql_string(call: ast.Call) -> str | None:
    if not call.args:
        return None
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
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
        sql = _first_sql_string(call)
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
        "executescript, outside txn_write/in_txn/configure_store_connection"
    ),
    scope=lambda *, exclude: (
        (path, tree)
        for path, tree in trees_under(_STORAGE_DIR)
        if "migrations" not in path.parts
        and path.name != "sqlite_store.py"
        and path.relative_to(REPO_ROOT).as_posix() not in _EXEMPT_MODULES
    ),
    skip=frozenset(_EXEMPT_MODULES),
    bite=lambda: (
        "import crawlutron.storage.safe_db as safe_db\n\n"
        "safe_db.txn_write(conn)\n"
        'conn.execute("UPDATE t SET x = 1")\n'
    ),
    clean=lambda: (
        'with txn_write(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'with in_txn(conn):\n    conn.execute("UPDATE t SET x = 1")\n',
        'def configure_store_connection(conn):\n    conn.execute("PRAGMA busy_timeout=5000")\n',
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
