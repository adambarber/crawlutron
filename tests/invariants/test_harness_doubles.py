"""The harness imports production declarations; it never re-declares them (#112).

A double is a second copy of a shape ``app/`` declares: two detectors govern, name
equality over every declaration form plus full-field-set shape for records.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from tests.chokepoint_walks import trees_under

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _model_declared_names() -> dict[str, str]:
    """Every public name app/models/ declares, mapped to the module that declares it."""
    models_root = REPO_ROOT / "app" / "models"
    declared: dict[str, str] = {}
    for path, tree in trees_under(models_root):
        for node in tree.body:
            for name in node_names(node):
                if not name.startswith("_"):
                    declared[name] = path.relative_to(REPO_ROOT).as_posix()
    return declared


def node_names(node: ast.stmt) -> list[str]:
    """The names one module-level statement binds (PEP 695 aliases included)."""
    if isinstance(node, ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
        return [node.name.id]
    return []


def _decorator_name(decorator: ast.expr) -> str:
    """The bare name a decorator resolves to (``@dataclass(frozen=True)`` too)."""
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    if isinstance(decorator, ast.Call):
        return _decorator_name(decorator.func)
    return ""


def _record_shape(node: ast.ClassDef) -> frozenset[str] | None:
    """Field names of a dataclass, TypedDict or pydantic BaseModel class, or None.

    Inherited fields are excluded on purpose: a subclass REUSES the production
    declaration, which is the desired direction, not a second copy of it.
    """
    is_dataclass = any(_decorator_name(d) == "dataclass" for d in node.decorator_list)
    is_typed_dict = any(_base_name(b) == "TypedDict" for b in node.bases)
    is_pydantic = any(_base_name(b) == "BaseModel" for b in node.bases)
    if not is_dataclass and not is_typed_dict and not is_pydantic:
        return None
    fields: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            fields.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    fields -= {name for name in fields if name.startswith("model_")}
    return frozenset(fields) if len(fields) >= 2 else None


def _base_name(base: ast.expr) -> str:
    """The bare name a class base resolves to (``pydantic.BaseModel`` too)."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _declared_shapes(root: pathlib.Path) -> dict[frozenset[str], list[str]]:
    """Field shapes of record-like classes under ``root``, mapped to declaring modules."""
    shapes: dict[frozenset[str], list[str]] = {}
    for path, tree in trees_under(root):
        module = path.relative_to(REPO_ROOT).as_posix()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            shape = _record_shape(node)
            if shape is not None:
                shapes.setdefault(shape, []).append(module)
    return shapes


def _tests_declared_names() -> dict[str, list[str]]:
    """Names a tests/ module DECLARES (class or alias), mapped to declaring modules."""
    declared: dict[str, list[str]] = {}
    for path, tree in trees_under(REPO_ROOT / "tests"):
        module = path.relative_to(REPO_ROOT).as_posix()
        for node in tree.body:
            for name in node_names(node):
                if not name.startswith("_"):
                    declared.setdefault(name, []).append(module)
    return declared


def test_a_tests_module_never_declares_a_name_app_models_declares() -> None:
    """The #112 name gate: a declared double is a finding, module named."""
    models = _model_declared_names()
    assert "EngineCrawlResult" in models and "NormalizedUrl" in models, (
        "the model census came back empty or blind; the detector would govern nothing"
    )
    assert "JsonValue" in models, (
        "app/models declares JsonValue via a PEP 695 type alias; a census blind "
        "to that form cannot govern a tests/ module re-declaring it"
    )
    doubles = _tests_declared_names()
    findings = {name: (modules, models[name]) for name, modules in doubles.items() if name in models}
    assert not findings, f"harness doubles of app/models/ declarations: {findings}"


def test_a_tests_module_never_mirrors_a_production_field_shape() -> None:
    """A tests/ record CONTAINING a production field set is a re-declaration.

    Superset-or-equal fires (a grown double is still the copy); a subset stays
    silent. Governed: dataclass, TypedDict, pydantic BaseModel.
    """
    model_shapes = _declared_shapes(REPO_ROOT / "app" / "models")
    assert model_shapes, "the shape census came back empty; the detector would govern nothing"
    findings: dict[tuple[str, ...], tuple[list[str], list[str]]] = {}
    for shape, modules in _declared_shapes(REPO_ROOT / "tests").items():
        for model_shape, model_modules in model_shapes.items():
            if shape >= model_shape:
                findings[tuple(sorted(shape))] = (modules, model_modules)
    assert not findings, f"harness records containing app/models/ field sets: {findings}"


def test_the_admission_helper_declares_the_production_known_set() -> None:
    """``admit(known)`` names production's type, never a set[str] that
    re-implements the admission boundary."""
    from tests._policy_factories import admit

    parameter = inspect.signature(admit).parameters["known"]
    annotation = inspect.signature(admit).parameters["known"].annotation
    assert "NormalizedUrl" in str(annotation) and "str" not in str(annotation).replace("NormalizedUrl", ""), (
        f"admit(known) drifted from production's set[NormalizedUrl]: {parameter}"
    )


def test_the_canned_result_builder_declares_the_record_s_url_union() -> None:
    """The builder declares the record's own union (text or value) -- the
    arg-type mypy reported where a NormalizedUrl reached a str parameter."""
    import inspect

    from tests._result_builders import _canned_result

    annotation = str(inspect.signature(_canned_result).parameters["url"].annotation)
    assert "NormalizedUrl" in annotation and "str" in annotation, (
        f"_canned_result(url) drifted from the record's own union: {annotation}"
    )
