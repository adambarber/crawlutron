"""H1 drift guard: never re-derive env numbers without the parser.

Bare `int()`/`float()` on an env-sourced mapping read is the config-typo
crash the env parser (`app/validation.py:get_env_int`/`get_env_float`)
exists to absorb -- it raised at import and denied boot. This AST scan
keeps every app module on the safe path.

Two shapes are banned, because name-aliasing defeats shape-only matching
(``source = os.environ; int(source.get(...))`` slipped past the original
single-shape detector):

1. ``int/float(<anything>.get("BROWSER_CRAWLER_..."))`` -- the constant
   env-key prefix is the robust signal regardless of how the mapping is
   aliased (settings.py:from_mapping blinds spot, OSS-review H1).
2. ``int/float(os.environ.get(...))`` -- the direct shape, any key name.
3. ``var = <env read>; ... int(var)`` -- the alias read-back. The original
   bypass for this shape lived in resource_pressure.derive_browser_pool_size
   (``explicit = os.environ.get(...); int(explicit)``), which slipped past
   shapes 1-2 because the coercion targets the *variable*, not the call.
"""

import ast

from chokepoint_walks import app_trees, nodes_of, offender_at


def _is_attr_get(node: ast.AST) -> bool:
    """True for a ``<anything>.get(...)`` call -- the shape both env faces match on.

    The two detectors below differ only in what they then require of the
    receiver and the argument, so the call shape itself is stated once.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"


def _is_env_get(node: ast.AST) -> bool:
    """True for ``os.environ.get(...)`` -- the direct env face."""
    return (
        _is_attr_get(node)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
    )


def _is_env_source(node: ast.AST) -> bool:
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
    bad = ast.parse(
        'import os\n\ndef f():\n    raw = os.environ.get("BROWSER_CRAWLER_X")\n    return int(raw)\n'
    )
    assert _env_get_aliases(bad) == {"raw"}
    ok = ast.parse('def f(limits):\n    raw = limits.get("max_bytes")\n    return int(raw)\n')
    assert not _env_get_aliases(ok)
    subscript = ast.parse(
        'import os\n\ndef f():\n    raw = os.environ["BROWSER_CRAWLER_X"]\n    return int(raw)\n'
    )
    assert _env_get_aliases(subscript) == {"raw"}


def _is_browser_crawler_get(node: ast.AST) -> bool:
    """True for ``<mapping>.get("BROWSER_CRAWLER_...")`` under any alias."""
    return (
        _is_attr_get(node)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("BROWSER_CRAWLER_")
    )


def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        env_aliases = _env_get_aliases(tree)
        for node in nodes_of(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in ("int", "float"):
                continue
            for arg in node.args:
                if _is_browser_crawler_get(arg):
                    offenders.append(
                        offender_at(path, node, f"bare {node.func.id} on BROWSER_CRAWLER_* mapping read")
                    )
                elif _is_env_get(arg):
                    offenders.append(offender_at(path, node, f"bare {node.func.id} on os.environ.get"))
                elif isinstance(arg, ast.Name) and arg.id in env_aliases:
                    offenders.append(
                        offender_at(path, node, f"bare {node.func.id} on env-read alias {arg.id!r}")
                    )
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)
