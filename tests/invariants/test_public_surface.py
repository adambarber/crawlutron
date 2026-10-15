"""A property owner declares one public surface, and adding a second is a change.

The registry tracks how much public surface each owner module has -- how many
public symbols, how many entry points -- and this file is the half of that story
that fails when the number grows. A second public surface is not a style
preference: it is a second way for a caller to reach the same owner, and a rule
that holds through one door and not the other is a rule that can be walked around.

The budget is a count of offenders rather than a threshold on a count, because
a threshold can be satisfied forever by choosing the threshold. At zero the
budget does the failing on its own, and the offender list is in the failure text,
so the gate and the work list are the same artifact -- there is no separate report
to fall out of date. Both numbers are pinned by the tests below, which is what
makes "the surface grew" a red run instead of a reviewer's memory.
"""

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
    "app/admission/admission_links.py": (10, 4),
    # RAISED to (14, 11) then LOWERED to (7, 6), (5, 5), (4, 4), then (2, 2): HostPolicy and
    # registrable_domain
    "app/safety/validation.py": (2, 2),
    "app/config/env.py": (7, 5),
    # RAISED to (7, 6): failed_unified_result is the single failure projection.
    "app/api/response_shaping.py": (7, 6),
    "app/api/unified_shaping.py": (5, 5),
    "app/storage/sqlite_store.py": (9, 7),
    "app/safety/safe_regex.py": (7, 2),
    "app/orchestration/robots.py": (12, 3),
    "app/safety/bounded_io.py": (4, 2),
    "app/transport/decompression.py": (5, 2),
    "app/admission/credentials.py": (3, 3),
    "app/admission/render_options.py": (2, 2),
    # RAISED to (2, 2): `payload_policy` was `_payload_policy` until the layout put its only production caller
    # (the engine) in another package.
    "app/admission/payload_helpers.py": (3, 3),
    # RAISED to (10, 9): terminal_attempt became public when CrawlResult stopped copying four axes out of the
    # attempt it holds.
    "app/orchestration/classification.py": (3, 2),
    "app/orchestration/engine_result.py": (8, 7),
    "app/orchestration/render_result.py": (4, 4),
    "app/transport/fetch_policy.py": (5, 5),
    "app/models/host_policy.py": (6, 5),
}


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


def _surface_budget_offenders(*, grown: bool) -> list[str]:
    """Ratchet offenders for the public-surface budgets, by direction.

    Both directions read the same counts against the same budgets, so they share
    this walk: a budget that is too loose is as much a defect as one that is too
    tight, and two hand-rolled loops over the ratchet is one more place for the
    two to disagree about what the budget is.
    """
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

    A class, a PEP 695 ``type`` alias, a ``NewType``, and a bare assignment
    (``JsonValue = None | bool | ...``) are all the same statement of "this module
    declares a type", and the rule that a declaration is findable from its name has
    to hold for all four or it is decorative for three of them. Module constants
    are not declarations: ``LITERAL_FIELDS`` is data the module uses, not a name a
    reader goes looking up, so SCREAMING_CASE and ``__all__`` are excluded.
    """
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

    A trailing ``Error`` is a naming convention rather than a distinct concept, so
    ``BrowserPoolExhaustedError`` and ``browser_pool_exhausted.py`` are one
    declaration, not two spellings. Encoding the suffix here means the exception
    is stated once instead of being noticed and special-cased per class.
    """
    stem = re.sub(r"(?<!^)(?=[A-Z])", "_", declared).lower().removesuffix("_error")
    return f"{stem}.py"


def _models_home_offenders(root: pathlib.Path = _MODELS_ROOT) -> list[str]:
    """Declarations whose home is not findable, and declarations with two homes.

    Two checks, because they are two different failures. A declaration in a module
    named for something else cannot be looked up from its name -- the rule
    ``app/models/__init__.py`` states. The same declaration in two modules can be
    looked up from either and so has no home at all, which is the failure that one
    concept per file was split to prevent and the one nothing else would notice.

    The naming check applies to a module holding exactly one declaration, which is
    the case where the name is unambiguous and the reader has the best chance of
    guessing right. A module holding several is named for the family: ``crawl_worker.py``
    holds the sync and async forms of one worker, and ``crawl_result.py`` holds a
    result and the attempt it records. Demanding any member's name there would mean
    renaming the file to hide one of its own declarations. That boundary is the
    honest limit of the rule -- a multi-declaration module can still collect a
    declaration that belongs next door -- and the duplicate check below is what
    catches the failure that matters in one.
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

    ``from app.models import X`` makes a type importable from anywhere, which is
    what would let it live in the wrong file and still import cleanly -- the exact
    drift the one-concept-per-file rule exists to make impossible. The empty
    ``__init__`` is the mechanism, so this is the gate that keeps it empty.
    """
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


def test_models_concern_modules_still_exist():
    """The exemptions above must name files that exist and still hold no type.

    An exemption is a hole in a rule, so the hole is itself checked: a module
    removed or renamed leaves a stale entry that would silently exempt whatever
    took its place.
    """
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
    Run against a synthetic tree, not the live one, so the proof does not depend
    on ``app/models`` happening to contain a violation: a bite planted with a live
    name stops biting the moment that name is renamed, which is precisely the fix
    the rule drives.
    """
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
