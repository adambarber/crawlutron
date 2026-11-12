"""The harness imports production declarations; it never re-declares them (#112).

A double is a second copy of a shape ``app/`` declares, compared SUPERSET-OR-EQUAL (#412).
An INTERSECTING record (shares fields, holds no full set) stays silent.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import APP_ROOT, trees_under

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _model_declared_names() -> dict[str, str]:
    """Every public name crawlutron/models/ declares, mapped to the module that declares it."""
    models_root = APP_ROOT / "models"
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


_RECORD_BASE_TAILS = ("BaseModel", "TypedDict")

# Browser-family stand-in law (#844): each fake carries every member of the
# production Protocol it stands in for; both table ends are asserted live.
_FAKE_STANDINS = {
    "BrowserFakePage": "RenderPage",
    "ScriptedPage": "RenderPage",
    "FakePageContext": "GuardContext",
}


def _base_name(base: ast.expr) -> str:
    """The bare name a class base resolves to (``pydantic.BaseModel`` too)."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _own_fields(node: ast.ClassDef) -> frozenset[str]:
    """The field names one class body declares, pydantic's ``model_`` namespace dropped.

    A ``ClassVar`` annotation is a class attribute, not a field: pydantic excludes it
    and so does this census.
    """
    fields: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if _annotation_is_classvar(stmt.annotation):
                continue
            fields.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            fields.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    return frozenset(fields) - {name for name in fields if name.startswith("model_")}


def _annotation_is_classvar(annotation: ast.expr) -> bool:
    """Whether an annotation is (or wraps, under Annotated[...]) a ClassVar."""
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        tail = ""
        if isinstance(value, ast.Attribute):
            tail = value.attr
        elif isinstance(value, ast.Name):
            tail = value.id
        if tail == "ClassVar":
            return True
        if tail == "Annotated":
            return _annotation_is_classvar(annotation.slice)
    return False


def _module_census(tree: ast.Module) -> tuple[dict[str, ast.ClassDef], dict[str, str]]:
    """(class name -> node, local binding -> dotted origin) for one module.

    The binding table is what makes an ALIASED record base readable: after
    ``from pydantic import BaseModel as BM``, ``BM`` binds ``pydantic.BaseModel``.
    """
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return classes, bindings


class _RecordCensus:
    """Record resolution over one root's parsed modules.

    A class is a record when it wears ``@dataclass`` or some base binds a record base
    -- directly, aliased via the import table, or through a class that itself does.
    """

    def __init__(self, trees: list[tuple[pathlib.Path, ast.Module]]) -> None:
        self.modules: dict[str, tuple[dict[str, ast.ClassDef], dict[str, str]]] = {}
        for path, tree in trees:
            self.modules[path.relative_to(REPO_ROOT).as_posix()] = _module_census(tree)
        self._record_memo: dict[tuple[str, str], bool] = {}
        self._fields_memo: dict[tuple[str, str], frozenset[str]] = {}

    def _origin_target(self, module: str, display: str) -> tuple[str, str] | None:
        """(defining module, class name) an imported base display resolves to."""
        origin = self.modules[module][1].get(display, "")
        if not origin or "." not in origin:
            return None
        defining, name = origin.rsplit(".", 1)
        target = defining.replace(".", "/") + ".py"
        if target in self.modules and name in self.modules[target][0]:
            return (target, name)
        return None

    def _base_binds_record(self, module: str, display: str, seen: frozenset[str]) -> bool:
        """Whether a written base name binds a record base, however it was imported."""
        if display in _RECORD_BASE_TAILS:
            return True
        origin = self.modules[module][1].get(display, "")
        if any(origin == f"pydantic.{tail}" or origin.endswith(f".{tail}") for tail in _RECORD_BASE_TAILS):
            return True
        classes = self.modules[module][0]
        if display in classes and self.is_record(module, display, seen):
            return True
        target = self._origin_target(module, display)
        return target is not None and self.is_record(target[0], target[1], seen)

    def is_record(self, module: str, name: str, seen: frozenset[str] = frozenset()) -> bool:
        """Whether the named class governs a field shape, transitively."""
        key = (module, name)
        if key in self._record_memo:
            return self._record_memo[key]
        if name in seen or name not in self.modules[module][0]:
            return False
        node = self.modules[module][0][name]
        result = any(_decorator_name(d) == "dataclass" for d in node.decorator_list) or any(
            self._base_binds_record(module, _base_name(base), seen | {name}) for base in node.bases
        )
        self._record_memo[key] = result
        return result

    def fields(self, module: str, name: str, seen: frozenset[str] = frozenset()) -> frozenset[str]:
        """Accumulated fields of a record: own body plus record-kind bases' fields.

        No size cutoff here -- the two-field floor is a shape-report decision, and a
        one-field base still contributes its field to the records that derive from it.
        """
        key = (module, name)
        if key in self._fields_memo:
            return self._fields_memo[key]
        if name in seen:
            return frozenset()
        classes = self.modules[module][0]
        fields: set[str] = set(_own_fields(classes[name]))
        for base in classes[name].bases:
            display = _base_name(base)
            base_module, base_name = module, display
            if base_name not in classes:
                target = self._origin_target(module, display)
                if target is None:
                    continue
                base_module, base_name = target
            if self.is_record(base_module, base_name):
                fields |= self.fields(base_module, base_name, seen | {name})
        result = frozenset(fields)
        self._fields_memo[key] = result
        return result

    def records(self) -> Iterator[tuple[str, str, frozenset[str]]]:
        """Every governed record with a shape (>=2 fields), as (module, name, fields)."""
        for module, (classes, _bindings) in self.modules.items():
            for name in classes:
                if not self.is_record(module, name):
                    continue
                shape = self.fields(module, name)
                if len(shape) >= 2:
                    yield (module, name, shape)


def _shapes_from_trees(trees: list[tuple[pathlib.Path, ast.Module]]) -> dict[frozenset[str], list[str]]:
    """Field shapes of record-like classes in already-parsed modules, mapped to modules."""
    shapes: dict[frozenset[str], list[str]] = {}
    for module, _name, shape in _RecordCensus(trees).records():
        shapes.setdefault(shape, []).append(module)
    return shapes


def _declared_shapes(root: pathlib.Path) -> dict[frozenset[str], list[str]]:
    """Field shapes of record-like classes under ``root``, mapped to declaring modules."""
    return _shapes_from_trees(list(trees_under(root)))


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
        "packages/core/src/crawlutron/models declares JsonValue via a PEP 695 type alias; a census blind "
        "to that form cannot govern a tests/ module re-declaring it"
    )
    doubles = _tests_declared_names()
    findings = {name: (modules, models[name]) for name, modules in doubles.items() if name in models}
    assert not findings, f"harness doubles of crawlutron/models/ declarations: {findings}"


def test_a_tests_module_never_mirrors_a_production_field_shape() -> None:
    """A tests/ record CONTAINING a production field set is a re-declaration.

    Superset-or-equal fires (a grown double is still the copy); a subset and a
    single-field record (<2 fields) stay silent. Governed transitively (#412).
    """
    model_shapes = _declared_shapes(APP_ROOT / "models")
    assert model_shapes, "the shape census came back empty; the detector would govern nothing"
    findings: dict[tuple[str, ...], tuple[list[str], list[str]]] = {}
    for shape, modules in _declared_shapes(REPO_ROOT / "tests").items():
        for model_shape, model_modules in model_shapes.items():
            if shape >= model_shape:
                findings[tuple(sorted(shape))] = (modules, model_modules)
    assert not findings, f"harness records containing crawlutron/models/ field sets: {findings}"


def _smallest_production_entry() -> tuple[frozenset[str], str, str]:
    """The alphabetically-first three-field production record: (shape, module, class),
    derived from the live census so the planted doubles aim at a record that exists."""
    entries: list[tuple[frozenset[str], str, str]] = []
    census = _RecordCensus(list(trees_under(APP_ROOT / "models")))
    for module, name, shape in census.records():
        entries.append((shape, module, name))
    assert entries, "no multi-field production record exists; re-derive the planted doubles"
    return min(entries, key=lambda item: (sorted(item[0]), item[1], item[2]))


def _planted_finding(source: str, label: str) -> tuple[list[str], list[str]] | None:
    """The finding one planted tests-side tree produces against the real census."""
    model_shapes = _declared_shapes(APP_ROOT / "models")
    planted = _shapes_from_trees([(REPO_ROOT / label, ast.parse(source))])
    for shape, modules in planted.items():
        for model_shape, model_modules in model_shapes.items():
            if shape >= model_shape:
                return (modules, model_modules)
    return None


def test_a_double_whose_base_is_an_aliased_import_fires() -> None:
    """The aliased-import hole (#412): ``from pydantic import BaseModel as BM`` put
    the record outside the detector's vocabulary, so a renamed double inherited
    through the alias passed. The planted double must fire naming both modules."""
    fields, production, _classname = _smallest_production_entry()
    body = "\n".join(f"    {field}: int" for field in sorted(fields))
    source = f"from pydantic import BaseModel as BM\n\nclass RenamedAliasedDouble(BM):\n{body}\n"
    finding = _planted_finding(source, "tests/_planted_double.py")
    assert finding is not None, "the aliased-import double did NOT fire; the hole is live"
    planted_modules, production_modules = finding
    assert planted_modules == ["tests/_planted_double.py"]
    # The planted shape may superset several production frames; any named
    # declaring module proves the detector found the production side.
    assert production_modules and all(
        m.startswith("packages/core/src/crawlutron/models/") for m in production_modules
    )


def test_a_double_split_across_a_local_record_base_fires() -> None:
    """The transitive hole (#412): a double split as ``_Base(BaseModel)`` plus a
    child carrying the rest of the fields re-declares production in two pieces;
    the direct-base check saw neither. The planted pair must fire naming both."""
    fields, production, _classname = _smallest_production_entry()
    head, *rest = sorted(fields)
    tail_fields = "\n".join(f"    {field}: int" for field in rest)
    source = (
        "from pydantic import BaseModel\n\n"
        f"class _SplitBase(BaseModel):\n"
        f"    {head}: int\n\n"
        f"class RenamedSplitDouble(_SplitBase):\n"
        f"{tail_fields}\n"
    )
    finding = _planted_finding(source, "tests/_planted_double.py")
    assert finding is not None, "the split transitive double did NOT fire; the hole is live"
    planted_modules, production_modules = finding
    assert planted_modules == ["tests/_planted_double.py"]
    assert production_modules and all(
        m.startswith("packages/core/src/crawlutron/models/") for m in production_modules
    )


def test_a_tests_side_reuse_of_a_production_record_stays_silent() -> None:
    """The clean half of the transitive fix: subclassing a PRODUCTION record is
    reuse (the declared direction), so its imported fields must not accumulate
    into a planted finding."""
    _fields, production, classname = _smallest_production_entry()
    source = (
        f"from {production[:-3].replace('/', '.')} import {classname}\n"
        "\n"
        f"class ReuseDouble({classname}):\n"
        f"    extra_field: int\n"
    )
    finding = _planted_finding(source, "tests/_planted_double.py")
    assert finding is None, f"reuse of the production record fired: {finding}"


def test_the_admission_helper_declares_the_production_known_set() -> None:
    """``admit(known)`` names production's type, never a set[str] that
    re-implements the admission boundary."""
    from tests._policy_factories import admit

    parameter = inspect.signature(admit).parameters["known"]
    annotation = inspect.signature(admit).parameters["known"].annotation
    assert "NormalizedUrl" in str(annotation) and "str" not in str(annotation).replace("NormalizedUrl", ""), (
        f"admit(known) drifted from production's set[NormalizedUrl]: {parameter}"
    )


def test_the_result_builder_declares_the_record_s_url_union() -> None:
    """The builder declares the record's own union (text or value) -- the
    arg-type mypy reported where a NormalizedUrl reached a str parameter."""
    import inspect

    from tests._result_builders import engine_result

    annotation = str(inspect.signature(engine_result).parameters["url"].annotation)
    assert "NormalizedUrl" in annotation and "str" in annotation, (
        f"engine_result(url) drifted from the record's own union: {annotation}"
    )


def test_the_browser_page_fakes_carry_the_production_protocol_surface() -> None:
    """The browser-family extension of the superset-or-equal law: a page or context double standing in for a
    ``crawlutron.transport.render_page`` Protocol must declare every member -- a fake that DROPPED one
    would fail inside a render phase instead of at this gate.
    """
    from tests._browser_double_surface import _protocol_members, _standin_members

    protocols = _protocol_members()
    assert "RenderPage" in protocols and "GuardContext" in protocols, (
        "the protocol census came back empty; the gate would govern nothing"
    )
    findings: list[str] = []
    for fake, protocol in sorted(_FAKE_STANDINS.items()):
        missing = protocols[protocol] - _standin_members(fake)
        if missing:
            findings.append(f"{fake} drops {sorted(missing)} of {protocol}")
    assert not findings, f"browser doubles missing production protocol members: {findings}"


def test_a_browser_fake_that_drops_a_production_member_reds() -> None:
    """The mutation check: a fake dropping a RenderPage member is a finding
    naming the fake, proved in memory over a mutated parse -- never by touching
    the workspace tree."""
    import pytest

    from tests._browser_double_surface import (
        _PAGE_DOUBLE_HOME,
        _protocol_members,
        _standin_members,
    )

    source = _PAGE_DOUBLE_HOME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "BrowserFakePage":
            node.body = [
                s
                for s in node.body
                if not (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "content")
            ]
            break
    else:
        pytest.fail("BrowserFakePage not found; re-derive the mutant")
    mutated = _PAGE_DOUBLE_HOME.with_name("_browser_page_doubles.held")
    mutated.write_text(ast.unparse(tree), encoding="utf-8")
    try:
        missing = _protocol_members()["RenderPage"] - _standin_members("BrowserFakePage", mutated)
    finally:
        mutated.unlink()
    assert "content" in missing, f"the dropped-member mutant did not discriminate: {missing}"


def test_the_browser_standin_table_is_live_on_both_ends() -> None:
    """Both ends of the stand-in table exist: every named fake is a class the
    page-double module declares, and every named protocol is one production
    declares. A renamed-away entry would silently ungovern its fake."""
    from tests._browser_double_surface import _page_double_classes, _protocol_members

    classes = _page_double_classes()
    protocols = _protocol_members()
    unknown = {fake: fake not in classes for fake in _FAKE_STANDINS} | {
        protocol: protocol not in protocols for protocol in _FAKE_STANDINS.values()
    }
    assert not any(unknown.values()), f"stand-in table drifts from the live tree: {unknown}"


def test_engine_result_carries_the_caller_s_link_records_once_each() -> None:
    """Every ``engine_result`` caller handing link records gets them carried
    once each; the result carries those records, never re-derived from
    strings (a re-wrap crashes and the engine swallows it)."""
    from tests._result_builders import GRAPH_ROOT, _link, engine_result

    links = [
        _link("http://example.test/b", GRAPH_ROOT),
        _link("http://example.test/c", GRAPH_ROOT),
    ]
    result = engine_result(GRAPH_ROOT, "rendered", raw_links=links)

    assert result.discovered_links == tuple(links), (
        "the builder must carry the caller's own link records, not re-derive them"
    )
    assert result.discovered_link_count == len(links), (
        f"each link appears exactly once, got {result.discovered_link_count}"
    )
    assert result.status == "success", result.status
