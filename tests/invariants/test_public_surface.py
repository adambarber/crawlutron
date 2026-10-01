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

from tests.chokepoint_walks import REPO_ROOT, assignment_targets, relative_posix
from tests.invariants._shared import _declared_public_symbols, assert_property_detector_bites
from tests.property_registry import Property

_PUBLIC_SURFACE_PROPERTY = Property(
    name="one-public-door",
    # Deliberately not in PROPERTY_REGISTRY, and deliberately not pointing at an
    # app module. The registry models "this app module owns this property", and
    # this constraint has no owner: `one-public-door` is enforced over the 13
    # modules of _OWNER_PUBLIC_SURFACE_RATCHET at once, each declaring its own
    # __all__ and adding no new entry point. Naming sqlite_store.py as the owner
    # was the original mistake -- it is one module of thirteen, and the claim is
    # false. The registry's owner check requires definition_site's symbol to be
    # declared in owner_symbols AND to resolve inside app/, so a descriptor whose
    # definition site is a test-tree detector cannot be registered without
    # asserting an ownership that does not exist.
    #
    # This object exists only because assert_property_detector_bites takes a
    # Property and reads its .name for the failure text. It is a detector
    # descriptor, not a registry entry.
    owner_module="tests/invariants/test_registry.py",
    owner_symbols=("_public_surface_offenders",),
    definition_site="tests/invariants/test_registry.py:_public_surface_offenders",
    fingerprints=("public_surface",),
    reason="A property owner declares one public surface and cannot add another entry point.",
)


# Values are (declared public symbols, executable entry points). Public data
# classes, exceptions, type aliases, and constants remain part of the contract,
# but only functions and classes with their own methods are alternate ways in.
#
# app/bounded_io.py LOWERED to (4, 2): read_capped_sync is gone. It existed only
# for the document extractor's urllib path, and that path is now the shared pinned
# httpx transport like every other fetch, so the module has one reader rather
# than a sync one and an async one that nothing used side by side.
#
# app/api/response_shaping.py RAISED to (9, 8) for the same reason: `EnvelopeOptions`
# is the one record the sync and async envelope builders both take, and it exists
# because the two builders each restated the same six optional parameters in
# their signatures -- six more places to add an option and forget one, and the
# copy-paste gate was holding the pair as a same-file clone. Folding them into a record is what
# made the builders agree by construction; a narrower budget would push the
# options back out into two hand-maintained lists.
#
# app/validation.py RAISED to (17, 11) for `in_range`. The int face of the env
# chokepoint could not REFUSE an out-of-range value -- only clamp it -- because
# the `validate`/`validate_event` pair that says "warn and use the fallback"
# existed on the float face and had never been exposed on the int face. So an
# operator who configured a deployment value outside its contract got a
# different number in silence, while the code's own comment promised "falls back
# here, loudly". `in_range` is the predicate that pair consumes; it is the
# module's job because a range is a validation fact and a second copy of it in
# settings.py is what the drifted literal `hi=120` already was once. Declared in
# `__all__` rather than left module-private, because a private name imported
# across a module boundary is a public name with a lie on it.
#
# The entry-point half then DROPPED 13 -> 11, and that is the more interesting
# half: `get_env_int`/`get_env_float` are no longer second copies of the
# mapping faces' signatures, they are the mapping faces bound to `os.environ`.
# Adding `validate`/`validate_event` to one face had pushed the other face's
# restated parameter list past the clone gate, which is the gate correctly
# catching a signature that was maintained in two places. Two faces of one
# chokepoint should be one declaration, not two.
_OWNER_PUBLIC_SURFACE_RATCHET = {
    "app/crawl/admission.py": (20, 14),
    "app/validation.py": (17, 11),
    "app/api/response_shaping.py": (9, 8),
    "app/crawl/sqlite_store.py": (9, 7),
    "app/safe_regex.py": (7, 2),
    "app/crawl/robots.py": (13, 4),
    "app/bounded_io.py": (4, 2),
    "app/crawl/decompression.py": (3, 1),
    "app/crawl/credentials.py": (3, 3),
    "app/crawl/render_options.py": (2, 2),
    "app/safe_url.py": (1, 1),
    "app/crawl/payload_helpers.py": (1, 1),
    "app/crawl/result_helpers.py": (1, 0),
    "app/crawl/strategies/fetch_policy.py": (5, 5),
}


# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from
# attacker-influenced text (the ReDoS shape). `re.search(LITERAL, text)` is
# safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = frozenset({"compile", "search", "match", "fullmatch", "sub", "findall", "split"})


def _owned_public_symbols(tree: ast.Module) -> set[str]:
    public: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                public.add(node.name)
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
        quiet=(REPO_ROOT / "app" / "crawl" / "credentials.py").read_text(encoding="utf-8"),
    )
