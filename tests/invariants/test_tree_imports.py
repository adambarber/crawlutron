"""Test imports use package paths; private names stay in their package."""

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    TESTS_DIR,
    from_imports,
    offender_at,
    plain_imports,
    relative_posix,
    trees_under,
)
from tests.invariants.test_tree_immutability import _tests_tree
from tests.rules import Bites, Rule, Silent

# Measured on tests/invariants/ alone: 37.7s of a 49.4s file, led by
# test_property_registry_fingerprints_are_observed 8.06s, test_documented_browser_knobs_have_consumers


def _app_reads(monkeypatch) -> list[pathlib.Path]:
    """Record every `read_text` of a file under app/ for the rest of this test."""
    original = pathlib.Path.read_text
    seen: list[pathlib.Path] = []

    def counting(self: pathlib.Path, *args, **kwargs) -> str:
        if self.is_relative_to(APP_ROOT):
            seen.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counting)
    return seen


def _test_tree_module_names() -> frozenset[str]:
    """The bare module names that name a file in the test tree.

    Cached because the sweep asks once per import statement and the set does not
    change within a run -- the same reasoning as the shared ``app_trees``
    snapshot, and it is what keeps this affordable at every call site.
    """
    return frozenset(candidate.stem for candidate in TESTS_DIR.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every module name a tree imports, with the node that imported it.

    ``from a.b import c`` contributes ``a.b``; ``import a.b`` contributes
    ``a.b`` as written. One traversal over both spellings so the caller compares
    one thing rather than repeating its verdict per import form.
    """
    for from_node in from_imports(tree):
        if from_node.module:
            yield from_node, from_node.module
    # A distinct name from the loop above: the two are separate traversals of the same tree, and one binding
    # for both meant the second yield carried the first loop's node type.
    for plain_node in plain_imports(tree):
        for alias in plain_node.names:
            yield plain_node, alias.name


def _bare_sibling_import_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A test module importing a sibling helper by its bare name.

    ``from _chain_policies import chain_policy`` resolves only because pytest
    puts each test file's own directory on ``sys.path``. That is the conftest
    path hack in a form E402 and mypy cannot see: the module is unresolvable to
    the type checker, and the import breaks the moment the file moves up a
    directory or the rootdir changes. Two test modules had it, both reading
    exactly like the package imports around them.

    Membership is decided by asking whether the name is a file in the test tree,
    not by listing the standard library -- so ``httpx`` and ``pytest`` are left
    alone by construction rather than by an allowlist that would need a third
    dependency added to it. The match is on the whole name because no module in
    the test tree is dotted, so a dotted import cannot be one of these.
    """
    local = _test_tree_module_names()
    return [
        offender_at(
            path,
            node,
            f"imports the test helper '{module}' by bare name; use its package path",
        )
        for node, module in _imported_modules(tree)
        if module in local
    ]


PLANTED_BARE_IMPORT = (
    "from _chain_policies import chain_policy\n"
    "import _job_harnesses as harnesses\n"
    "from _policy_factories import _policy\n"
)


def _own_package(module: str) -> str:
    """The package a dotted module name belongs to.

    ``app.transport.http`` -> ``app.transport``; a top-level module
    like ``app.runtime.metrics`` -> ``app``. A module name is never its own package, so a
    leaf module and its siblings share one boundary.

    That last clause is why ``app.runtime.cgroup_sense`` and ``app.runtime.memory_pressure`` are
    one package rather than two. Reading a top-level module as its own package
    would exempt every one of the top-level modules from each other, and those
    are exactly the reaches hardest to see -- there is no directory name to
    suggest a boundary has been crossed.
    """
    head, _, _tail = module.rpartition(".")
    return head


def _dotted_module(path: pathlib.Path) -> str:
    """``app/admission/admission.py`` -> ``app.admission.admission``.

    Root-relative, because the import being judged is written root-relative and
    a comparison between an absolute path and a dotted name never matches. The
    failure that produces is quiet rather than loud: every top-level module looks
    like it sits outside every other package, so the rule fires on reaches that
    are inside the same one and a reader stops believing it.
    """
    return relative_posix(path).removesuffix(".py").replace("/", ".")


def _private_leak_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A private app name imported from outside the package that defines it.

    A leading underscore has two halves and a name is only honest if both are
    respected. The conventional half is package-internal: ``_post_fetch_diagnostics``
    is private to ``app.transport`` and every sibling module may use it,
    because no consumer outside ``transport`` can be written ``from
    app.transport.http import _post_fetch_diagnostics`` and get far. The
    half that gets broken is the one the import form defeats -- binding the name
    into another package *does* make it reachable, and the underscore is then
    asserting a boundary the reader cannot check.

    Twenty-one names were doing this. ``DNS_EXECUTOR`` lives in
     ``transport.pinned_transport`` and is imported by ``api.webhooks``;
    ``allow_private_targets`` lives in ``app.models.host_policy`` and is imported
    by a transport strategy; ``BROWSER_RAM_FRACTION`` crosses from ``app`` into
    ``runtime.resource_pressure``. Each is a real collaboration, which is
    precisely why the fix is to promote the name rather than to forbid the call.

    The boundary is the *imported-from* module, not a map from name to definer, so
    this reads one import and decides it. That is what lets it be a ``Rule``: a
    detector that had to know where the name was born would need the whole corpus
    to answer, and a proof that plants a synthetic source could not supply it --
    the bite would pass for any implementation, since the synthetic name would be
    absent from the real map. Comparing the two packages on the node in hand is
    both cheaper and stricter: a re-exporting hop is judged against the hop that
    names it, and a name laundered twice is caught on the first.
    """
    module = _dotted_module(path)
    here = _own_package(module)
    return [
        offender_at(
            path,
            node,
            f"imports '{alias.name}' from '{node.module}', which is outside the "
            f"'{here}' package that defines it; a name another package needs is not private",
        )
        for node in from_imports(tree)
        if node.module and node.module.startswith("app") and _own_package(node.module) != here
        for alias in node.names
        if alias.name.startswith("_") and not alias.name.startswith("__")
    ]


#: ``app/api/render.py``: in ``app.api``, so anything under ``app.orchestration`` is outside its package.
_RENDER_PROOF_PATH = REPO_ROOT / "app" / "api" / "render.py"


#: A real sibling of ``transport.content_pipeline``, so the private import in the clean proof resolves inside
#: one package and must stay quiet.
_TRANSPORT_SIBLING_PROOF_PATH = REPO_ROOT / "app" / "transport" / "browser_result.py"


PRIVATE_NAME_LEAKS_PACKAGE = Rule(
    name="a-private-app-name-is-not-importable-outside-its-package",
    find=_private_leak_offenders,
    summary=(
        "a leading underscore means package-internal: promote the name if another package needs it, "
        "rather than importing it across the boundary where the convention no longer holds"
    ),
    scope=lambda *, exclude: trees_under(APP_ROOT),
    bite=lambda: Bites(
        # Names that do NOT exist in app/.
        "from app.transport.http import _unpromoted_strategy_result\n"
        "from app.safety.validation import _unpromoted_private_target_gate\n",
        marker="which is outside the",
        count=2,
        at=_RENDER_PROOF_PATH,
    ),
    clean=lambda: (
        # A sibling in the defining package: private, and reachable only from here.
        Silent(
            "from app.transport.content_pipeline import _unpromoted_sibling_detail\n",
            at=_TRANSPORT_SIBLING_PROOF_PATH,
        ),
        # Public cross-package: no underscore, so no boundary is being crossed.
        "from app.vocabulary.identity import USER_AGENT_TOKEN\n",
        # dunder spellings are not privacy, they are protocol hooks.
        "from app.transport.http import __all__\n",
        # stdlib and third-party are outside the rule entirely.
        "import httpx\nfrom pathlib import Path\n",
    ),
)


def test_a_private_name_does_not_leave_its_package():
    """No app module reaches a private name in another package.

    The load-bearing half of a name that is genuinely private, and the thing a
    reviewer reads ``from app.models.host_policy import allow_private_targets`` and
    cannot check at a glance.
    """
    PRIVATE_NAME_LEAKS_PACKAGE.scan()


def test_private_leak_detector_bites():
    """Bites across the boundary; silent inside it, and silent on the rest.

    The clean case pins all four ways the rule must not degenerate: a sibling
    inside the defining package, a public cross-package name, a dunder spelling,
    and imports that are not app-internal at all. Without them the rule would
    also pass as "no imports whose name starts with an underscore anywhere",
    which is a different and much worse rule.
    """
    PRIVATE_NAME_LEAKS_PACKAGE.check_bites()


TEST_HELPERS_IMPORTED_BY_PACKAGE_PATH = Rule(
    name="test-helpers-imported-by-package-path",
    find=_bare_sibling_import_offenders,
    summary=(
        "import the helper through its package path (tests.<pkg>.<module>): a bare sibling name "
        "resolves only because pytest put this file's directory on sys.path, which is invisible to "
        "both E402 and the type checker"
    ),
    scope=_tests_tree,
    bite=lambda: Bites(PLANTED_BARE_IMPORT, marker="imports the test helper", count=3),
    clean=lambda: (
        # Package, stdlib and third-party forms name a module that is not a test file, so the rule cannot
        # degenerate into "no unqualified imports".
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "import httpx\n"
        "import pytest\n"
        "from tests._policy_factories import _policy\n"
        "from tests.crawl._chain_policies import chain_policy\n"
        "from app.admission.policy import read_policy\n",
    ),
)


def test_tests_import_their_helpers_by_package_path():
    """No test module reaches a sibling helper by bare name.

    Tree-wide over ``tests/``, so a helper added tomorrow is covered by existing
    the same way, and read from the tree rather than a file list.
    """
    TEST_HELPERS_IMPORTED_BY_PACKAGE_PATH.scan()


def test_bare_sibling_import_detector_bites():
    """Bites: the shipped spellings, and the neighbors that must stay quiet.

    The first is the real thing -- the exact ``from _chain_policies import
    chain_policy`` that two test modules carried. The clean case pins both
    directions: the package form, a stdlib import, and a third-party import all
    name a module that is not a test file and must be left alone, so the rule
    cannot degenerate into "no unqualified imports".
    """
    TEST_HELPERS_IMPORTED_BY_PACKAGE_PATH.check_bites()
