"""Joined URLs are egress-filtered; the frontier grows through the funnel."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    app_trees,
    assignment_targets,
    enclosing_function,
    functions,
    nodes_of,
    offender_at,
    parent_map,
)
from tests.invariants._shared import _SCOPE_COMPILER_NAMES, _SCOPE_PATTERN_LENGTH_BOUND
from tests.rules import Bites, Rule

# --------------------------------------------------------------------------- Unfiltered urljoin egress doors
# (H class) detect_pagination read an href out of attacker-controlled HTML and returned

_EGRESS_FILTERS = frozenset({"admit_or_none", "resolve"})


def _is_urljoin_call(node: ast.expr) -> bool:
    """True for a plain ``urljoin(...)`` call.

    Takes the ``ast.expr`` its callers hold (an assignment's value, a return's
    value) rather than the ``ast.AST`` the walk produced, and narrows inside.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin"


def _unfiltered_urljoin_return_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a function returning a ``urljoin`` result with no egress filter -- a URL
    out of untrusted text is an egress door once trusted, so the join and the filter
    belong together, and "every consumer remembers" does not scale.
    """
    offenders: list[str] = []
    for func in functions(tree):
        joined: set[str] = set()
        returns_raw = False
        returns_binding = False
        for node in nodes_of(func):
            if isinstance(node, ast.Assign) and node.value is not None and _is_urljoin_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        joined.add(target.id)
            elif isinstance(node, ast.Return) and node.value is not None:
                if _is_urljoin_call(node.value):
                    returns_raw = True
                elif isinstance(node.value, ast.Name) and node.value.id in joined:
                    returns_binding = True
        if not (returns_raw or returns_binding):
            continue
        uses_filter = any(
            (isinstance(n, ast.Name) and n.id in _EGRESS_FILTERS)
            or (isinstance(n, ast.Attribute) and n.attr in _EGRESS_FILTERS)
            for n in nodes_of(func)
        )
        if not uses_filter:
            offenders.append(
                offender_at(path, func, f"{func.name}() returns a urljoin with no egress filter")
            )
    return offenders


JOINED_URLS_ARE_EGRESS_FILTERED = Rule(
    name="joined-urls-are-egress-filtered",
    find=_unfiltered_urljoin_return_offenders,
    summary=(
        "A URL resolved out of untrusted text becomes an egress door as soon as a "
        "caller trusts it, so the join and the filter belong together; filter "
        "joined URLs through NormalizedUrl.resolve / admit_or_none"
    ),
    bite=lambda: (
        Bites(
            "def detect_pagination(html, final_url):\n    return urljoin(final_url, href)\n",
            "no egress filter",
        ),
        # A urljoin bound to a name and returned is the same leak.
        Bites(
            "def f(base, href):\n    u = urljoin(base, href)\n    return u\n",
            "no egress filter",
        ),
    ),
    clean=lambda: ("def f(base, href):\n    return base.resolve(href)\n",),
)


def test_no_unfiltered_urljoin_returns():
    """A URL joined from untrusted input is egress-filtered before it leaves."""
    JOINED_URLS_ARE_EGRESS_FILTERED.scan()


def test_unfiltered_urljoin_detector_bites():
    """The historical leak shape fires; the filtered and sanctioned shapes stay clean."""
    JOINED_URLS_ARE_EGRESS_FILTERED.check_bites()


def _definitions_in(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The scope-compiler labels one parsed tree contributes, split out from
    :func:`_scope_compiler_definitions` so the bites case can hand the detector a
    synthetic tree -- the walk owns ``app/`` only, so a firing proof plants its own.
    """
    found: list[str] = []
    for node in nodes_of(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in _SCOPE_COMPILER_NAMES:
                found.append(offender_at(path, node, f"scope-regex compiler {node.name}"))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _SCOPE_PATTERN_LENGTH_BOUND
            for target in node.targets
        ):
            found.append(offender_at(path, node, "scope-regex length bound"))
    return found


def _scope_compiler_definitions() -> list[str]:
    """File labels of every scope-regex compiler and of its length bound."""
    found: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        found.extend(_definitions_in(tree, path))
    return found


def test_scope_regex_compiler_has_exactly_one_definition():
    """Behavior parity across the dedupe is covered by tests/orchestration/test_scope_regex.py;
    this pins that no second copy of the compiler or of its bound can creep back."""
    found = _scope_compiler_definitions()
    compilers = [label for label in found if "compiler" in label]
    bounds = [label for label in found if "length bound" in label]
    assert len(compilers) == 1, f"scope-regex compiler duplicated again: {compilers}"
    assert len(bounds) == 1, f"scope-regex length bound duplicated again: {bounds}"
    assert "app/admission/admission_links.py" in compilers[0], compilers
    assert "app/admission/admission_links.py" in bounds[0], bounds


def test_scope_regex_compiler_detector_bites():
    """The detector reports one label per definition, so the tree it must
    reject is any tree with more than one -- two compilers, or the one
    compiler plus a second copy of its bound."""
    path = REPO_ROOT / "app" / "crawl" / "admission.py"
    two_compilers = ast.parse(
        "def compile_scope_pattern(value):\n    return value\n"
        "def _compile_scope_regex_safe(value):\n    return value\n"
    )
    compiler_and_bound = ast.parse(
        "_MAX_SCOPE_PATTERN_LEN = 256\ndef compile_scope_pattern(value):\n    return value\n"
    )
    assert len(_definitions_in(two_compilers, path)) == 2
    assert len(_definitions_in(compiler_and_bound, path)) == 2
    assert not _definitions_in(ast.parse("def _helper(value):\n    return value\n"), path)


def _frontier_expression(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "frontier"


# The admission chokepoint owns the pattern it bans: admit_job_links is the one function allowed to grow a
# frontier, so the sweep that bans direct growth everywhere else has to skip the module that defines it.
_ADMISSION_CHOKEPOINT = "app/admission/admission_links.py"


def _direct_frontier_growth_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    allowed_functions: frozenset[str] = frozenset(),
) -> list[str]:
    growth_methods = {"append", "extend", "insert"}
    offenders: list[str] = []
    parents = parent_map(tree)

    def is_allowed(node: ast.AST) -> bool:
        if not allowed_functions:
            return False
        owner = enclosing_function(node, parents)
        if (
            not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
            or owner.name not in allowed_functions
        ):
            return False
        if owner.name == "normalize_job_state":
            return (
                isinstance(node, ast.Assign)
                and any(_frontier_expression(target) for target in node.targets)
                and isinstance(node.value, ast.Name)
                and node.value.id == "frontier"
            )
        if owner.name in {"_admit_chunk", "admit_discovered_links"}:
            # The admission chokepoint is the funnel's sanctioned owner.
            return True
        return (
            owner.name == "_persist"
            and isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and _frontier_expression(node.value)
        )

    for node in nodes_of(tree):
        if is_allowed(node):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in growth_methods and _frontier_expression(node.func.value):
                offenders.append(offender_at(path, node, "direct frontier growth bypasses admit_job_links"))
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = assignment_targets(node)
            if any(_frontier_expression(target) for target in targets):
                offenders.append(offender_at(path, node, "frontier replacement bypasses admit_job_links"))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
                if _frontier_expression(node.value):
                    offenders.append(offender_at(path, node, "frontier alias bypasses admit_job_links"))
                if node.value.attr == "append" and _frontier_expression(node.value.value):
                    offenders.append(
                        offender_at(path, node, "frontier method alias bypasses admit_job_links")
                    )
        elif isinstance(node, ast.Subscript) and _frontier_expression(node.value):
            parent = parents.get(id(node))
            if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign):
                offenders.append(offender_at(path, node, "frontier slice mutation bypasses admit_job_links"))
    return offenders


JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL = Rule(
    name="job-frontier-growth-only-uses-funnel",
    find=_direct_frontier_growth_offenders,
    summary=(
        "Grow whole-site frontiers only through admit_job_links: a direct append, "
        "replacement, alias or slice mutation elsewhere re-opens the funnel every "
        "other entry point is collapsed through"
    ),
    skip=frozenset({"app/admission/admission.py"}),  # admit_job_links lives here
    kwargs_for=lambda relative: (
        {"allowed_functions": frozenset({"normalize_job_state"})}
        if relative == "app/storage/job_store_state.py"
        else {"allowed_functions": frozenset({"_persist"})}
        if relative == "app/storage/job_store.py"
        else {"allowed_functions": frozenset({"admit_discovered_links", "_admit_chunk"})}
        if relative == "app/admission/admission_links.py"
        else {}
    ),
    proof_path=REPO_ROOT / "app" / "api" / "job_runner.py",
    bite=lambda: (
        Bites(
            "def run(job, url):\n    job.frontier.append(url)\n",
            "direct frontier growth bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    append = job.frontier.append\n    append(url)\n",
            "frontier method alias bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    job.frontier += [url]\n",
            "frontier replacement bypasses admit_job_links",
        ),
        Bites(
            "def run(job, urls):\n    job.frontier = urls\n",
            "frontier replacement bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    frontier = job.frontier\n    frontier.append(url)\n",
            "frontier alias bypasses admit_job_links",
        ),
    ),
    clean=lambda: ("async def run(job, links):\n    await admit_job_links(job, links)\n",),
)


def test_job_frontier_growth_only_uses_funnel():
    JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL.scan()


def test_job_frontier_growth_detector_bites():
    JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL.check_bites()


_FILTER_NAMES = {
    "app.vocabulary.hosts.is_analytics_host": "is_analytics_host",
    "app.vocabulary.hosts.is_document_url": "is_document_url",
    "app.admission.scope_match.scope_allows": "scope_allows",
}


def _imported_filter_names(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes_of(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app.vocabulary.hosts",
            "app.admission.scope_match",
        }:
            for imported in node.names:
                canonical = f"{node.module}.{imported.name}"
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
        elif isinstance(node, ast.Import):
            for imported in node.names:
                canonical = imported.name
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
    return aliases


def _shared_admission_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    assert isinstance(tree, ast.Module)
    aliases = _imported_filter_names(tree)
    parents = parent_map(tree)
    function_calls: dict[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] = {}
    filter_names = set(_FILTER_NAMES.values())
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = aliases.get(node.func.id, node.func.id)
        if name not in filter_names:
            continue
        owner = enclosing_function(node, parents)
        if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            function_calls.setdefault(owner, set()).add(name)
    offenders: list[str] = []
    for function, names in function_calls.items():
        if len(names) >= 2:
            offenders.append(offender_at(path, function, "recursive admission filter list is re-inlined"))
    return offenders


SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST = Rule(
    name="shared-admission-filter-set-has-no-twin-list",
    find=_shared_admission_filter_offenders,
    summary=(
        "The is_analytics_host / is_document_url / scope_allows filter ladder is one "
        "shared list; a function re-inlining more than one of them by hand is a "
        "twin that drifts from the chokepoint\u2019s future edits"
    ),
    skip=frozenset({_ADMISSION_CHOKEPOINT}),
    bite=lambda: Bites(
        "def expand(links, visited, scope):\n"
        "    candidates = []\n"
        "    for link in links:\n"
        "        if link.url in visited: continue\n"
        "        if is_analytics_host(link.url): continue\n"
        "        if is_document_url(link.url): continue\n"
        "        if scope is not None and not scope_allows(scope, link.url): continue\n"
        "        candidates.append(link)\n"
        "    return candidates\n",
        "recursive admission filter list is re-inlined",
    ),
    clean=lambda: ("async def expand(links):\n    return await admit_discovered_links(links)\n",),
)


def test_shared_admission_filter_set_has_no_twin_list():
    SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST.scan()


def test_shared_admission_filter_detector_bites():
    SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST.check_bites()


def test_job_frontier_detector_sanction_is_narrow():
    path = REPO_ROOT / "app" / "crawl" / "job_store.py"
    allowed = frozenset({"normalize_job_state", "_persist"})
    bad = ast.parse("def grow_frontier(job):\n    job.frontier.append('x')\n")
    assert _direct_frontier_growth_offenders(bad, path, allowed_functions=allowed)
    sanctioned = ast.parse("def normalize_job_state(job):\n    frontier = []\n    job.frontier = frontier\n")
    assert not _direct_frontier_growth_offenders(sanctioned, path, allowed_functions=allowed)
