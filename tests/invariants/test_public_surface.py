"""A property owner declares one public surface, and adding a second is a change.

The budget is an offender count -- at zero it fails by itself and the offenders
ARE the work list; pinned here, so "the surface grew" is a red run."""

from __future__ import annotations

import ast
import pathlib
import re

from tests.chokepoint_walks import REPO_ROOT, assignment_targets, relative_posix
from tests.invariants._shared import _declared_public_symbols, assert_property_detector_bites
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


# Values are (declared public symbols, executable entry points).
_OWNER_PUBLIC_SURFACE_RATCHET = {
    "app/admission/admission.py": (9, 9),
    "app/admission/admission_links.py": (11, 5),
    # RAISED to (14, 11) then LOWERED to (7, 6), (5, 5), (4, 4), then (2, 2): HostPolicy and
    # registrable_domain
    "app/safety/validation.py": (2, 2),
    "app/config/env.py": (7, 5),
    # RAISED to (7, 6): failed_unified_result is the single failure projection.
    "app/api/response_shaping.py": (7, 6),
    "app/api/unified_shaping.py": (5, 5),
    "app/storage/sqlite_store.py": (9, 7),
    "app/safety/safe_regex.py": (7, 2),
    # robots_pattern_to_regex is public (#190) -- robots_allows itself calls it -- so the
    # parity/per-host tests drive that real contract; the matcher class behind it stays private.
    "app/orchestration/robots.py": (13, 4),
    "app/safety/bounded_io.py": (4, 2),
    "app/transport/decompression.py": (5, 2),
    "app/admission/credentials.py": (3, 3),
    "app/admission/render_options.py": (2, 2),
    # RAISED to (2, 2): `payload_policy` was `_payload_policy` until the layout put its only production caller
    # (the engine) in another package.
    "app/admission/payload_helpers.py": (2, 2),
    # RAISED to (10, 9): terminal_attempt became public when CrawlResult stopped copying four axes out of the
    # attempt it holds.
    "app/orchestration/classification.py": (3, 2),
    # Designed RAISE: the engine and render result funnels were merged into one
    # result-construction surface (single home, single import run) per #324.
    "app/orchestration/results.py": (12, 11),
    "app/transport/fetch_policy.py": (5, 5),
    "app/models/host_policy.py": (6, 5),
    # The two latest surface modules: the gate must not silently drop a module
    # added since the list was pinned (these prune handlers/helpers via __all__).
    "app/api/keys_router.py": (4, 3),
    "app/api/crawl_endpoint.py": (4, 4),
    # #127: the pool concept splits on a real async/sync axis -- AsyncPool for
    # BrowserPool, SyncPool for DocExtractionPool -- so the surface is two protocols.
    "app/orchestration/pool.py": (2, 2),
}


# Every other module declaring a literal ``__all__`` is an explicit, self-validating
# exemption: an entry must still declare ``__all__``, closing the unenumerated-module hole (#215).
_SURFACE_DECLARING_EXEMPT = frozenset(
    {
        "app/admission/policy.py",
        "app/api/_depends_singletons.py",
        "app/api/async_crawl_runner.py",
        "app/api/caller_auth.py",
        "app/api/caller_concurrency.py",
        "app/api/crawl_executor.py",
        "app/api/crawl_router.py",
        "app/api/dependencies.py",
        "app/api/exceptions.py",
        "app/api/identity_router.py",
        "app/api/job_runner.py",
        "app/api/jobs_router.py",
        "app/api/main_helpers.py",
        "app/api/map_endpoint.py",
        "app/api/metrics_router.py",
        "app/api/middleware.py",
        "app/api/policy_defaults.py",
        "app/api/policy_router.py",
        "app/api/policy_suggestions.py",
        "app/api/render.py",
        "app/api/render_endpoint.py",
        "app/api/watch_sweep.py",
        "app/api/webhooks.py",
        "app/extraction/content.py",
        "app/extraction/llm_format.py",
        "app/mcp/constants.py",
        "app/mcp/protocol.py",
        "app/mcp/router.py",
        "app/mcp/server.py",
        "app/mcp/tools.py",
        "app/orchestration/admission_stall.py",
        "app/orchestration/recursive.py",
        "app/orchestration/request_pipeline.py",
        "app/orchestration/robots_parse.py",
        "app/orchestration/strategy_chain.py",
        "app/orchestration/worker.py",
        "app/runtime/logging_config.py",
        "app/runtime/resource_pressure.py",
        "app/search/endpoint.py",
        "app/search/providers.py",
        "app/storage/coordination_cookies.py",
        "app/storage/coordination_counters.py",
        "app/storage/coordination_store.py",
        "app/storage/job_store.py",
        "app/storage/job_store_collections.py",
        "app/storage/job_store_state.py",
        "app/storage/migrations/runner.py",
        "app/storage/watch_store.py",
        "app/transport/browser.py",
        "app/transport/browser_cookies.py",
        "app/transport/browser_result.py",
        "app/transport/content_pipeline.py",
        "app/transport/http_client_pool.py",
        "app/transport/strategy_request.py",
    }
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
        if _declared_public_symbols(ast.parse(path.read_text(encoding="utf-8"))) is None:
            continue
        found.add(rel)
    return found


def test_public_surface_gate_covers_or_exempts_every_all_module():
    """The hand-pinned enumeration must not silently drop a surface module added
    since it was written: every module with ``__all__`` is either ratchet'd (budgeted)
    or explicitly exempted, so a new module in neither fails this gate (#215)."""
    covered = set(_OWNER_PUBLIC_SURFACE_RATCHET) | _SURFACE_DECLARING_EXEMPT
    uncovered = sorted(_all_surface_modules() - covered)
    assert not uncovered, (
        "module(s) declare a public surface via __all__ but are not in the ratchet or the "
        "exemption list: " + ", ".join(uncovered)
    )


def test_surface_exemptions_are_self_validating():
    """An exemption must still declare ``__all__`` to stay exempt; a removed or renamed
    module leaves a stale entry that silently exempts nothing and this test fails it."""
    stale = sorted(_SURFACE_DECLARING_EXEMPT - _all_surface_modules())
    assert not stale, "exempted modules no longer declare __all__ (or no longer exist): " + ", ".join(stale)


def _surface_budget_offenders(*, grown: bool) -> list[str]:
    """Ratchet offenders for the public-surface budgets, by direction.

    Both directions read the same counts against the same budgets, so they
    share this walk: too-loose is as much a defect as too-tight."""
    offenders: list[str] = []
    for relative, (surface_budget, entry_budget) in _OWNER_PUBLIC_SURFACE_RATCHET.items():
        path = REPO_ROOT / relative
        surface_count, entry_count = _owner_surface_counts(ast.parse(path.read_text(encoding="utf-8")))
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


def test_public_surface_detector_bites():
    historical_decompression_owner = (
        REPO_ROOT / "tests" / "fixtures" / "safety" / "historical_decompression_owner.txt"
    ).read_text(encoding="utf-8")
    assert_property_detector_bites(
        _PUBLIC_SURFACE_PROPERTY,
        _public_surface_offenders,
        caught={"uncapped decompression at ca425fc": (historical_decompression_owner, 1)},
        quiet=(REPO_ROOT / "app" / "admission" / "credentials.py").read_text(encoding="utf-8"),
    )


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
        declared_types = _declared_types(ast.parse(path.read_text(encoding="utf-8")))
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
    tree = ast.parse((root / "__init__.py").read_text(encoding="utf-8"))
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
        declared = _declared_types(ast.parse(path.read_text(encoding="utf-8")))
        public_types = {name for name in declared if not name.startswith("_")}
        if len(public_types) > 1:
            offenders.append(f"{path.name} co-locates {sorted(public_types)}")
    return offenders


#: One-concept families pending the full per-type collapse: a union plus its
#: members, or a model plus the helper it declares. Fully split in #121's remainder.
_COHESIVE_MODULES = frozenset(
    {
        "crawl_diagnostics.py",  # CrawlDiagnostics + RuntimeDiagnostics: the bag and its runtime block
        "crawl_result.py",  # CrawlResult + EngineCrawlResult: the shared result family
        "crawl_worker.py",  # CrawlWorker = SyncCrawlWorker | AsyncCrawlWorker
        "fetch_envelope.py",  # SerializedResponse + its RetryDiagnostics
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
        tree = ast.parse((_MODELS_ROOT / name).read_text(encoding="utf-8"))
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
