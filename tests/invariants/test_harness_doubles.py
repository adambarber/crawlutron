"""The harness imports production declarations; it never re-declares them (#112).

A double is a second copy of a shape ``app/`` declares -- wrong or becoming
wrong. mypy sees only a contradicting double, so the detector below governs.
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
    for path, _tree in trees_under(models_root):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.ClassDef, ast.Assign, ast.AnnAssign)):
                for name in node_names(node):
                    if not name.startswith("_"):
                        declared[name] = path.relative_to(REPO_ROOT).as_posix()
    return declared


def node_names(node: ast.stmt) -> list[str]:
    """The names one module-level statement binds."""
    if isinstance(node, ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _tests_declared_names() -> dict[str, list[str]]:
    """Names a tests/ module DECLARES (class or alias), mapped to declaring modules."""
    declared: dict[str, list[str]] = {}
    for path, _tree in trees_under(REPO_ROOT / "tests"):
        source = path.read_text(encoding="utf-8")
        module = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.parse(source).body:
            for name in node_names(node):
                if not name.startswith("_"):
                    declared.setdefault(name, []).append(module)
    return declared


def test_a_tests_module_never_declares_a_name_app_models_declares() -> None:
    """The #112 detector: a declared double is a finding, module named."""
    models = _model_declared_names()
    assert "EngineCrawlResult" in models and "NormalizedUrl" in models, (
        "the model census came back empty or blind; the detector would govern nothing"
    )
    doubles = _tests_declared_names()
    findings = {name: (modules, models[name]) for name, modules in doubles.items() if name in models}
    assert not findings, f"harness doubles of app/models/ declarations: {findings}"


def test_the_double_detector_bites() -> None:
    """A planted double (a declared app/models/ name) is found, modules named."""
    models = _model_declared_names()
    assert "EngineCrawlResult" in models and "NormalizedUrl" in models, (
        "the model census came back empty or blind; the detector would govern nothing"
    )
    planted = ast.parse("class NormalizedUrl:\n    pass\n\nNormalizedUrlAlias = NormalizedUrl\n")
    planted_names: list[str] = []
    for node in planted.body:
        planted_names.extend(node_names(node))
    assert any(name in models for name in planted_names), (
        "the detector's own trigger shape stopped matching a planted double"
    )


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
