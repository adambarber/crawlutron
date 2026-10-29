"""A property owner declares one public surface, and adding a second is a change.

The budget is an offender count -- at zero it fails by itself and the offenders
ARE the work list; pinned here, so "the surface grew" is a red run."""

from __future__ import annotations

import ast
import pathlib
import re

from tests.chokepoint_walks import REPO_ROOT, assignment_targets, parsed, relative_posix
from tests.invariants._shared import _declared_public_symbols
from tests.invariants._surface_ratchet import (
    DECLARED_SURFACE_RATCHET as _DECLARED_SURFACE_RATCHET,
)
from tests.invariants._surface_ratchet import (
    OWNER_PUBLIC_SURFACE_RATCHET as _OWNER_PUBLIC_SURFACE_RATCHET,
)
from tests.property_registry import Property

_PUBLIC_SURFACE_PROPERTY = Property(
    name="one-public-door",
    # Deliberately not in PROPERTY_REGISTRY, and deliberately not pointing at an app module.
    owner_module="tests/invariants/test_registry.py",
    owner_symbols=("_public_surface_offenders",),
    definition_site="tests/invariants/test_registry.py:_public_surface_offenders",
    fingerprints=("public_surface",),
    reason="A property owner declares one public surface and cannot add another entry point.",
)

# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from attacker-influenced text (the
# ReDoS shape). `re.search(LITERAL, text)` is safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = frozenset({"compile", "search", "match", "fullmatch", "sub", "findall", "split"})


def _owned_public_symbols(tree: ast.Module) -> set[str]:
    public: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                public.add(node.name)
        elif isinstance(node, ast.TypeAlias):
            # ``type X = ...`` (PEP 695) is a declaration of X at module level, and is what replaced ``X:
            # TypeAlias = ...``
            if not node.name.id.startswith("_"):
                public.add(node.name.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = assignment_targets(node)
            public.update(
                target.id
                for target in targets
                if isinstance(target, ast.Name) and not target.id.startswith("_")
            )
    return public


def _entry_point_symbols(tree: ast.Module) -> set[str]:
    entry_points: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            entry_points.add(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) for child in node.body):
                entry_points.add(node.name)
    return entry_points


def _public_surface_offenders(tree, path, _prop, *, modules=None) -> list[str]:
    relative = relative_posix(path)
    actual = _owned_public_symbols(tree)
    declared = _declared_public_symbols(tree)
    if declared is None:
        return [f"{relative}: public symbols are not declared by one literal __all__"]
    if actual == declared:
        return []
    missing = ", ".join(sorted(actual - declared)) or "none"
    extra = ", ".join(sorted(declared - actual)) or "none"
    return [f"{relative}: __all__ mismatch (missing: {missing}; extra: {extra})"]


def _owner_surface_counts(tree: ast.Module) -> tuple[int, int]:
    return len(_owned_public_symbols(tree)), len(_entry_point_symbols(tree))


def test_property_owner_public_surfaces_equal_all():
    offenders: list[str] = []
    for relative in _OWNER_PUBLIC_SURFACE_RATCHET:
        path = REPO_ROOT / relative
        tree = parsed(path)
        offenders.extend(_public_surface_offenders(tree, path, _PUBLIC_SURFACE_PROPERTY))
    assert not offenders, "property owner public surface drifted: " + ", ".join(offenders)


def _all_surface_modules(root: pathlib.Path = REPO_ROOT / "app") -> set[str]:
    """Every module (outside ``app/models``) that declares a literal ``__all__``.
    app/models is governed by the models section below, not this gate.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = relative_posix(path)
        if path.name == "__init__.py" or rel.startswith("app/models/"):
            continue
        if _declared_public_symbols(parsed(path)) is None:
            continue
        found.add(rel)
    return found


def test_public_surface_gate_covers_every_all_module():
    """Every module declaring ``__all__`` is ratchet-budgeted, unconditionally: a new
    surface module not in a ratchet fails this gate (#215). Both ratchets live
    in tests/invariants/_surface_ratchet.py (#476)."""
    covered = set(_OWNER_PUBLIC_SURFACE_RATCHET) | set(_DECLARED_SURFACE_RATCHET)
    uncovered = sorted(_all_surface_modules() - covered)
    assert not uncovered, (
        "module(s) declare a public surface via __all__ but have no ratchet budget row: "
        + ", ".join(uncovered)
    )


def _surface_budget_offenders(*, grown: bool) -> list[str]:
    """Ratchet offenders for the public-surface budgets, by direction.

    Both directions read the same counts against the same budgets, so they
    share this walk: too-loose is as much a defect as too-tight."""
    offenders: list[str] = []
    for ratchet in (_OWNER_PUBLIC_SURFACE_RATCHET, _DECLARED_SURFACE_RATCHET):
        for relative, (surface_budget, entry_budget) in ratchet.items():
            path = REPO_ROOT / relative
            surface_count, entry_count = _owner_surface_counts(parsed(path))
            over = surface_count > surface_budget or entry_count > entry_budget
            under = surface_count < surface_budget or entry_count < entry_budget
            if over if grown else under:
                offenders.append(
                    f"{relative}: surface {surface_count}/{surface_budget}, "
                    f"entry points {entry_count}/{entry_budget}"
                )
    return offenders


def test_property_owner_public_surface_counts_do_not_grow():
    offenders = _surface_budget_offenders(grown=True)
    assert not offenders, "property owner public surface grew: " + ", ".join(offenders)


def test_property_owner_public_surface_budgets_are_tight():
    stale = _surface_budget_offenders(grown=False)
    assert not stale, "property owner budgets are stale; lower them to: " + ", ".join(stale)


# --------------------------------------------------------------------------- app/models: one home per
# declaration, and that home is the file's name

_MODELS_ROOT = REPO_ROOT / "app" / "models"


# The two modules here that hold a concern rather than a type.
_MODELS_CONCERN_MODULES = frozenset({"taxonomy.py", "serialization.py"})


def _declared_types(tree: ast.Module) -> set[str]:
    """The type declarations a module holds, in every form one can take.

    A class, ``type`` alias, ``NewType`` and bare assignment are the same
    "declares a type" statement; findability-from-name must hold for all four."""
    declared: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            declared.add(node.name)
        elif isinstance(node, ast.TypeAlias):
            declared.add(node.name.id)
        elif isinstance(node, ast.Assign):
            declared.update(
                target.id
                for target in assignment_targets(node)
                if isinstance(target, ast.Name) and not _is_module_constant(target.id)
            )
    return declared


def _is_module_constant(name: str) -> bool:
    return name.startswith("_") or (name.isupper() and "_" in name)


def _home_module(declared: str) -> str:
    """The file a reader looking up ``declared`` should find.

    Trailing ``Error`` is a convention, so ``BrowserPoolExhaustedError`` and
    ``browser_pool_exhausted.py`` are one declaration, stated once here."""
    stem = re.sub(r"(?<!^)(?=[A-Z])", "_", declared).lower().removesuffix("_error")
    return f"{stem}.py"


def _models_home_offenders(root: pathlib.Path = _MODELS_ROOT) -> list[str]:
    """Declarations whose home is not findable, or that have two homes: a mis-named
    module cannot be looked up from its name, and a two-module declaration has no
    home at all -- the naming check applies to single-declaration modules.
    """
    offenders: list[str] = []
    homes: dict[str, str] = {}
    for path in sorted(root.glob("*.py")):
        if path.name == "__init__.py":
            continue
        declared_types = _declared_types(parsed(path))
        # A private class is not a name a reader goes looking up, for the same reason a module constant is
        # not; only public declarations pick the module's name.
        public_types = {name for name in declared_types if not name.startswith("_")}
        sole = len(public_types) == 1 and path.name not in _MODELS_CONCERN_MODULES
        for declared in sorted(public_types):
            if sole and _home_module(declared) != path.name:
                offenders.append(
                    f"{declared} lives in {path.name}; a reader looking up {declared} opens"
                    f" {_home_module(declared)}"
                )
        for declared in sorted(declared_types):
            if declared in homes:
                offenders.append(f"{declared} is declared in both {homes[declared]} and {path.name}")
            homes[declared] = path.name
    return offenders


def _models_reexport_offenders(root: pathlib.Path = _MODELS_ROOT) -> list[str]:
    """A declaration re-exported from ``__init__.py`` is a declaration with two homes.

    ``from app.models import X`` lets a type live in the wrong file and still
    import cleanly -- the drift one-concept-per-file makes impossible."""
    tree = parsed(root / "__init__.py")
    return [
        f"app/models/__init__.py imports "
        f"{', '.join(alias.name for alias in node.names)} -- re-exporting makes a type"
        " findable from anywhere, which is what the one-home rule forbids"
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def test_models_declarations_have_exactly_one_findable_home():
    assert not (offenders := _models_home_offenders()), "; ".join(offenders)


def test_models_package_reexports_nothing():
    assert not (offenders := _models_reexport_offenders()), "; ".join(offenders)


def _models_multitype_offenders(root: pathlib.Path = _MODELS_ROOT) -> list[str]:
    """A module that co-locates two or more unrelated public types -- a grab-bag
    no reader can find from a type's name. ``*_response.py``, concern and
    ``_COHESIVE_MODULES`` one-concept families are excluded, not grab-bags.
    """
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "__init__.py" or path.name.endswith("_response.py"):
            continue
        if path.name in _COHESIVE_MODULES or path.name in _MODELS_CONCERN_MODULES:
            continue
        declared = _declared_types(parsed(path))
        public_types = {name for name in declared if not name.startswith("_")}
        if len(public_types) > 1:
            offenders.append(f"{path.name} co-locates {sorted(public_types)}")
    return offenders


#: One-concept families pending the full per-type collapse: a union plus its
#: members, or a model plus the helper it declares. Fully split in #121's remainder.
_COHESIVE_MODULES = frozenset(
    {
        "crawl_diagnostics.py",  # CrawlDiagnostics + RuntimeDiagnostics: the bag and its runtime block
        "classification_evidence.py",  # ClassificationEvidence + its runtime TypedDict record
        "crawl_result.py",  # CrawlResult + EngineCrawlResult: the shared result family
        "crawl_worker.py",  # CrawlWorker = SyncCrawlWorker | AsyncCrawlWorker
        "fetch_envelope.py",  # SerializedResponse + its RetryDiagnostics
        "jscpd_report.py",  # the jscpd report family: Report, Statistics, Duplicate, Location
        "host_policy_suggestion.py",  # HostPolicySuggestion + CandidateRule
        "result_record.py",  # AnyResult union + Producer/RenderEnvelope
        "watch_state.py",  # WatchState + IntervalSeconds field helper
    }
)


def test_no_model_surface_co_locates_unrelated_types():
    """Nothing in ``app/models`` is a grab-bag of unrelated, co-located types:
    a type's name must find its home. The exceptions are a ``*_response.py`` wire
    projection, a concern module, and ``_COHESIVE_MODULES`` one-concept families.
    """
    assert not (offenders := _models_multitype_offenders()), "; ".join(offenders)


def test_models_concern_modules_still_exist():
    """The exemptions above must name files that exist and still hold no type.

    An exemption is a hole in a rule, so the hole is checked: a removed or
    renamed module leaves a stale entry silently exempting its replacement."""
    present = {path.name for path in _MODELS_ROOT.glob("*.py")}
    assert not _MODELS_CONCERN_MODULES - present, "exempted module no longer exists"
    for name in _MODELS_CONCERN_MODULES & present:
        tree = parsed(_MODELS_ROOT / name)
        assert not (classes := {n.name for n in tree.body if isinstance(n, ast.ClassDef)}), (
            f"{name} is exempt as holding a concern but declares {sorted(classes)};"
            " drop the exemption so the named-for-their-home rule governs it"
        )


def test_models_typing_detectors_bite(tmp_path):
    """Both detectors fire on the shapes they exist to forbid.

    Run against a synthetic tree, not the live one, so the proof cannot go
    silent when the fix renames a live name the bite was planted with."""
    (tmp_path / "crawl_task.py").write_text("class CrawlTask:\n    pass\n")
    (tmp_path / "records.py").write_text("class HttpFetchResult:\n    pass\n")
    assert _models_home_offenders(tmp_path) == [
        "HttpFetchResult lives in records.py; a reader looking up HttpFetchResult opens http_fetch_result.py"
    ]

    # Every alias form is covered, not just the class
    for source in (
        "class DiscoveredLink:\n    pass\n",
        "type DiscoveredLink = str\n",
        "DiscoveredLink = NewType('DiscoveredLink', str)\n",
    ):
        (tmp_path / "records.py").write_text(source)
        assert _models_home_offenders(tmp_path) == [
            "DiscoveredLink lives in records.py; a reader looking up DiscoveredLink opens discovered_link.py"
        ], source

    # Two homes for one declaration is the failure the split was for, and it is invisible to the naming check
    # -- each copy sits in a correctly named file.
    (tmp_path / "records.py").unlink()
    (tmp_path / "discovered_link.py").write_text("class DiscoveredLink:\n    pass\n")
    (tmp_path / "crawl_task.py").write_text(
        "class CrawlTask:\n    pass\n\n\nclass DiscoveredLink:\n    pass\n"
    )
    assert _models_home_offenders(tmp_path) == [
        "DiscoveredLink is declared in both crawl_task.py and discovered_link.py"
    ]

    # And an Error suffix is one declaration, not a second spelling of it.
    (tmp_path / "crawl_task.py").write_text("class CrawlTask:\n    pass\n")
    (tmp_path / "crawl_task.py").unlink()
    (tmp_path / "discovered_link.py").unlink()
    (tmp_path / "pool_exhausted.py").write_text("class PoolExhaustedError(Exception):\n    pass\n")
    assert _models_home_offenders(tmp_path) == []

    (tmp_path / "__init__.py").write_text('"""A barrel."""\n')
    assert _models_reexport_offenders(tmp_path) == []
    (tmp_path / "__init__.py").write_text(
        '"""A barrel."""\n\nfrom app.models.crawl_task import CrawlTask\n\n__all__ = ["CrawlTask"]\n'
    )
    assert _models_reexport_offenders(tmp_path) == [
        "app/models/__init__.py imports CrawlTask -- re-exporting makes a type findable"
        " from anywhere, which is what the one-home rule forbids"
    ]
    (tmp_path / "__init__.py").write_text('"""A barrel."""\n\nimport os\n')
    assert len(_models_reexport_offenders(tmp_path)) == 1, "a bare import is a re-export too"
