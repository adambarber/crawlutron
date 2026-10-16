"""Every SQLite store must share one durability configuration.

One function configures every store connection; a tree-wide invariant keeps
PRAGMA statements confined to it (PRAGMAs are connection-scoped)."""

import ast

from app.storage.result_store import CrawlResultStore
from tests._job_harnesses import make_job_store
from tests.chokepoint_walks import (
    app_trees,
    attribute_calls,
    nodes_of,
    single_target_name_assignments,
    string_argument,
)


def _pragma_exec_offenders() -> list[str]:
    """Any ``*.execute("...PRAGMA...")`` outside the sanctioned module."""
    found: list[str] = []
    for path, tree in app_trees():
        if path.name == "sqlite_store.py":
            continue
        for node, func in attribute_calls(tree):
            sql = string_argument(node) if func.attr == "execute" else None
            if sql is not None and "pragma" in sql.lower():
                found.append(f"{path.name}:{node.lineno}")
    return found


# --------------------------------------------------------------------------- Dynamic PRAGMA identifiers
# ---------------------------------------------------------------------------


def _dynamic_pragma_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """SQL assembled at runtime, in PRAGMA statements.

    Bandit's S608 gates built SQL but its keyword set is DML, so the one place
    a SQLite identifier is interpolated is where the linter is silent."""
    found: list[tuple[ast.Call, str]] = []
    for node, func in attribute_calls(tree):
        if func.attr not in {"execute", "executemany"} or not node.args:
            continue
        sql = node.args[0]
        if isinstance(sql, ast.Constant):
            continue
        if not _mentions_pragma(sql):
            continue
        shape = "f-string" if isinstance(sql, ast.JoinedStr) else type(sql).__name__
        found.append((node, shape))
    return found


def _mentions_pragma(sql: ast.AST) -> bool:
    """Whether an assembled SQL expression is a PRAGMA, however it was built.

    Joins the literal fragments and looks for the keyword in them, so a shape
    that hides the keyword behind concatenation is still recognised.
    """
    text = "".join(
        part.value for part in ast.walk(sql) if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ).lower()
    return "pragma" in text


def _is_identifier_chokepoint(node: ast.expr) -> bool:
    """Whether an expression is, or is derived from, the allowlist call.

    Narrowing inside lets every caller read ``node.func`` without repeating the
    ``isinstance`` that was invisible to them."""
    if not isinstance(node, ast.Call):
        return False
    if not (isinstance(node.func, ast.Name) and node.func.id == "_validated_identifier"):
        return False
    # `_validated_identifier(str(row[1]))` is the same guarantee: the argument is coerced, then allowlisted,
    # before it is ever spliced into a statement.
    return True


def _module_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to a literal.

    A PRAGMA interpolating one splices a constant into a *value*, not an
    identifier, and is not reachable from a request -- the allowlist would be
    theatre."""
    return {
        name
        for _node, name in single_target_name_assignments(
            tree.body, lambda value: isinstance(value, ast.Constant)
        )
    }


def _snippet_call(source: str, *, fn: int = 0, stmt: int = 0) -> ast.Call:
    """The ``Call`` evaluated by ``source``'s ``fn``-th function at ``stmt``.

    Every snippet is a tiny module whose body is one ``conn.execute(...)``; the
    walk is named so tests fail loudly if a snippet stops being its shape."""
    module = ast.parse(source)
    function = module.body[fn]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)), source
    statement = function.body[stmt]
    assert isinstance(statement, (ast.Expr, ast.Return)), source
    assert statement.value is not None, source
    assert isinstance(statement.value, ast.Call), source
    return statement.value


def _pragma_sql(call: ast.Call) -> ast.JoinedStr:
    """``call``'s first argument, asserted to be an f-string.

    Every snippet builds its PRAGMA with one -- the only shape the detector can
    read -- so a snippet that stopped being one fails as itself."""
    sql = call.args[0]
    assert isinstance(sql, ast.JoinedStr), ast.unparse(sql)
    return sql


def _local_chokepoint_bindings(function: ast.AST) -> set[str]:
    """Names in this function assigned the result of the allowlist call.

    The live shape validates into a local then interpolates it; checking only
    the inline call would manufacture the defect the detector exists to prevent."""
    return {
        name for _node, name in single_target_name_assignments(nodes_of(function), _is_identifier_chokepoint)
    }


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                return node
    return None


def _unvalidated_interpolations(tree: ast.Module, sql: ast.JoinedStr, at: ast.AST) -> list[str]:
    """Interpolated expressions that skip the identifier chokepoint.

    The allowlist is the whole defence (the argument is spliced verbatim): the
    call, a local bound to its result, or a module constant are accepted; a
    bare parameter is rejected."""
    safe = _module_constants(tree)
    function = _enclosing_function(tree, at)
    if function is not None:
        safe |= _local_chokepoint_bindings(function)
    unvalidated: list[str] = []
    for part in sql.values:
        if not isinstance(part, ast.FormattedValue):
            continue
        expression = part.value
        if _is_identifier_chokepoint(expression):
            continue
        if isinstance(expression, ast.Name) and expression.id in safe:
            continue
        unvalidated.append(ast.unparse(expression))
    return unvalidated


def _dynamic_pragma_offenders() -> list[str]:
    """A PRAGMA whose identifier is not allowlisted, or assembled outside the chokepoint."""
    found: list[str] = []
    for path, tree in app_trees():
        at_chokepoint = path.name == "sqlite_store.py"
        for node, shape in _dynamic_pragma_calls(tree):
            where = f"{path.name}:{node.lineno}"
            if not at_chokepoint:
                found.append(f"{where} builds a {shape} PRAGMA outside sqlite_store")
                continue
            assert isinstance(node.args[0], ast.JoinedStr), where
            for expression in _unvalidated_interpolations(tree, node.args[0], node):
                found.append(f"{where} interpolates {expression} without _validated_identifier")
    return found


def test_both_stores_share_the_pragma_configuration(tmp_path):
    for store in (
        make_job_store(tmp_path),
        CrawlResultStore(tmp_path / "results.db"),
    ):
        conn = store._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_store_pragmas_are_confined_to_the_configurator():
    """Adding a store means reusing the chokepoint, not re-typing the
    settings -- drifting them is exactly the defect."""
    offenders = _pragma_exec_offenders()
    assert not offenders, "PRAGMA outside sqlite_store.configure_store_connection: " + ", ".join(offenders)


def test_pragma_confinement_detector_bites():
    """The detector fires on the shape it bans (red-proof for the invariant)."""
    bad = ast.parse('conn.execute("PRAGMA journal_mode=WAL")')
    hits = [
        f"{n.lineno}"
        for n in nodes_of(bad)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "execute"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
        and "pragma" in n.args[0].value.lower()
    ]
    assert hits == ["1"]


def test_dynamic_pragma_identifiers_go_through_the_chokepoint():
    """Every PRAGMA identifier is allowlisted, in the one module allowed to build one."""
    offenders = _dynamic_pragma_offenders()
    assert not offenders, (
        "A PRAGMA splices its identifier into the statement verbatim; route it through "
        "_validated_identifier in sqlite_store.py, or write the statement as a literal: "
        + ", ".join(offenders)
    )


def test_dynamic_pragma_detector_bites():
    """Every way to get this wrong fires; the two live shapes stay clean."""
    # A bare parameter spliced straight into the statement: the actual injection.
    bare_source = "def f(conn, table):\n    conn.execute(f'PRAGMA table_info({table})')\n"
    bare = ast.parse(bare_source)
    call = _snippet_call(bare_source)
    assert _mentions_pragma(call.args[0])
    assert _unvalidated_interpolations(bare, _pragma_sql(call), call) == ["table"]

    # Concatenation, not an f-string, and the keyword is still recognized.
    concatenated = 'def f(conn, table):\n    conn.execute("PRAGMA index_info(" + table + ")")\n'
    parsed_concat = _snippet_call(concatenated)
    assert _mentions_pragma(parsed_concat.args[0])

    # Live shape 1: allowlisted inline.
    inline_source = (
        "def f(conn, table):\n    return conn.execute(f'PRAGMA table_info({_validated_identifier(table)})')\n"
    )
    inline = ast.parse(inline_source)
    inline_call = _snippet_call(inline_source)
    assert _mentions_pragma(inline_call.args[0])
    assert not _unvalidated_interpolations(inline, _pragma_sql(inline_call), inline_call)

    # Live shape 2: allowlisted into a local, then the local is interpolated.
    hoisted = ast.parse(
        "def f(conn, table, index_name):\n"
        "    table = _validated_identifier(table)\n"
        "    index_name = _validated_identifier(str(index_name))\n"
        '    return conn.execute(f"PRAGMA index_list({table})")\n'
        '    return conn.execute(f"PRAGMA index_info({index_name})")\n'
    )
    for node, _ in _dynamic_pragma_calls(hoisted):
        assert not _unvalidated_interpolations(hoisted, _pragma_sql(node), node)

    # A module constant is a pragma *value*, not an identifier (busy_timeout=5000).
    constant_source = (
        '_TIMEOUT = 5000\ndef f(conn):\n    return conn.execute(f"PRAGMA busy_timeout={_TIMEOUT}")\n'
    )
    constant = ast.parse(constant_source)
    constant_call = _snippet_call(constant_source, fn=1)
    assert not _unvalidated_interpolations(constant, _pragma_sql(constant_call), constant_call)

    # But hoisting a raw parameter into a local is not laundering: the binding has to be the chokepoint
    # itself, not any assignment at all.
    laundered_source = (
        "def f(conn, table):\n"
        "    table = str(table)\n"
        '    return conn.execute(f"PRAGMA table_info({table})")\n'
    )
    laundered = ast.parse(laundered_source)
    laundered_call = _snippet_call(laundered_source, stmt=-1)
    assert _unvalidated_interpolations(laundered, _pragma_sql(laundered_call), laundered_call) == ["table"]


def test_pragma_confinement_does_not_only_match_literals():
    """Why the literal-only check above is not enough on its own.

    The confinement detector requires ``ast.Constant``, so the assembled case it
    cannot see is the only one with an interpolated identifier."""
    source = ast.parse('conn.execute(f"PRAGMA table_info({table})")')
    seen_by_the_literal_check = [
        node
        for node in nodes_of(source)
        if isinstance(node, ast.Call)
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert not seen_by_the_literal_check, "the literal check now sees assembled PRAGMAs too"
    assert _dynamic_pragma_calls(source), "but the dynamic check must still catch it"
