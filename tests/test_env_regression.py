"""H1 drift guard: never re-derive env numbers without the parser.

Bare `int()`/`float()` on `os.environ.get(...)` is the config-typo crash the
env parser (`app/validation.py:get_env_int`/`get_env_float`) exists to absorb
-- it raised at import and denied boot. This AST scan keeps every app module
on the safe path.
"""

import ast

from chokepoint_walks import app_trees, offender


def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in ("int", "float"):
                continue
            for arg in node.args:
                if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)):
                    continue
                # match os.environ.get(...) as the sole argument
                value = arg.func.value
                if arg.func.attr == "get" and isinstance(value, ast.Attribute) and value.attr == "environ":
                    if isinstance(value.value, ast.Name) and value.value.id == "os":
                        offenders.append(
                            offender(path, node.lineno, f"bare {node.func.id} on os.environ.get")
                        )
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)
