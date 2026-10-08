"""Never re-derive env numbers without the parser.

Bare `int()`/`float()` on an env-sourced mapping read is the config-typo
crash the env parser (`app/safety/validation.py:get_env_int`/`get_env_float`)
exists to absorb -- it raised at import and denied boot. This AST scan
keeps every app module on the safe path.

Two shapes are banned, because name-aliasing defeats shape-only matching
(``source = os.environ; int(source.get(...))`` slipped past the original
single-shape detector):

1. ``int/float(<anything>.get("CRAWLUTRON_..."))`` -- the constant
   env-key prefix is the robust signal regardless of how the mapping is
   aliased (settings.py:from_mapping blinds spot).
2. ``int/float(os.environ.get(...))`` -- the direct shape, any key name.
3. ``var = <env read>; ... int(var)`` -- the alias read-back. The original
   bypass for this shape lived in resource_pressure.derive_browser_pool_size
   (``explicit = os.environ.get(...); int(explicit)``), which slipped past
   shapes 1-2 because the coercion targets the *variable*, not the call.
"""

import ast

from tests.chokepoint_walks import app_trees, bare_callee, call_nodes, nodes_of, offender_at


def _get_receiver(call: ast.Call) -> ast.expr | None:
    """The receiver of a ``<anything>.get(...)`` call, or None if it is not one.

    The call shape both env detectors below match on, stated once. It yields the
    receiver rather than answering yes/no, because a ``bool`` leaves the caller
    unable to narrow ``call.func`` and every call site re-spelled the shape after
    asking the question -- ``_is_attr_get(node) and node.func.value.attr`` could
    only be read by a type checker that did not exist.
    """
    if isinstance(call.func, ast.Attribute) and call.func.attr == "get":
        return call.func.value
    return None


def _is_env_get(node: ast.expr) -> bool:
    """True for ``os.environ.get(...)`` -- the direct env face."""
    if not isinstance(node, ast.Call):
        return False
    receiver = _get_receiver(node)
    return (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "environ"
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "os"
    )


def _is_env_source(node: ast.expr) -> bool:
    """Any direct env read: ``os.environ.get(...)``, ``os.getenv(...)``,
    ``os.environ[...]``."""
    if _is_env_get(node):
        return True
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getenv":
        return True
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    )


def _env_get_aliases(tree: ast.AST) -> set[str]:
    """Names assigned directly from an env read in this module's tree."""
    names: set[str] = set()
    for node in nodes_of(tree):
        if isinstance(node, ast.Assign) and _is_env_source(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_env_source(node.value):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def test_env_alias_escape_bites():
    """Shape 3 must fire on the read-back that shapes 1-2 structurally miss."""
    bad = ast.parse('import os\n\ndef f():\n    raw = os.environ.get("CRAWLUTRON_X")\n    return int(raw)\n')
    assert _env_get_aliases(bad) == {"raw"}
    ok = ast.parse('def f(limits):\n    raw = limits.get("max_bytes")\n    return int(raw)\n')
    assert not _env_get_aliases(ok)
    subscript = ast.parse(
        'import os\n\ndef f():\n    raw = os.environ["CRAWLUTRON_X"]\n    return int(raw)\n'
    )
    assert _env_get_aliases(subscript) == {"raw"}


def _is_crawlutron_get(node: ast.expr) -> bool:
    """True for ``<mapping>.get("CRAWLUTRON_...")`` under any alias."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    first = node.args[0]
    return (
        _get_receiver(node) is not None
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith("CRAWLUTRON_")
    )


def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        env_aliases = _env_get_aliases(tree)
        for node in call_nodes(tree):
            callee = bare_callee(node)
            if callee not in ("int", "float"):
                continue
            for arg in node.args:
                if _is_crawlutron_get(arg):
                    offenders.append(offender_at(path, node, f"bare {callee} on CRAWLUTRON_* mapping read"))
                elif _is_env_get(arg):
                    offenders.append(offender_at(path, node, f"bare {callee} on os.environ.get"))
                elif isinstance(arg, ast.Name) and arg.id in env_aliases:
                    offenders.append(offender_at(path, node, f"bare {callee} on env-read alias {arg.id!r}"))
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)
