"""H1 regression: bare int()/float() on os.environ.get() must never reappear."""
import ast, pathlib, sys
APP = pathlib.Path(__file__).parent.parent / "app"

BAD_PATTERNS = [
    ("re", "int(os.environ.get"),
    ("re", "float(os.environ.get"),
]

def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # int(os.environ.get(...)) pattern
                if isinstance(node.func, ast.Name) and node.func.id in ("int", "float"):
                    for arg in node.args:
                        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute):
                            if arg.func.attr == "get" and isinstance(arg.func.value, ast.Name) and arg.func.value.id == "environ":
                                if isinstance(arg.func.value.value, ast.Name) and arg.func.value.value.id == "os":
                                    offenders.append(f"{path}:{node.lineno} bare {node.func.id} on os.environ.get")
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)
