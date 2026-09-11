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
"""

import ast

from chokepoint_walks import app_trees, offender


def _is_env_get(node: ast.AST) -> bool:
    """True for ``os.environ.get(...)`` -- the direct env face."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
    )


def _is_browser_crawler_get(node: ast.AST) -> bool:
    """True for ``<mapping>.get("BROWSER_CRAWLER_...")`` under any alias."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("BROWSER_CRAWLER_")
    )


def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in ("int", "float"):
                continue
            for arg in node.args:
                if _is_browser_crawler_get(arg):
                    offenders.append(
                        offender(
                            path,
                            node.lineno,
                            f"bare {node.func.id} on BROWSER_CRAWLER_* mapping read",
                        )
                    )
                elif _is_env_get(arg):
                    offenders.append(offender(path, node.lineno, f"bare {node.func.id} on os.environ.get"))
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)
