"""The suite reaches an ``app`` module through a public name, or through the ledger.

43 of the 49 structural commits that moved ``app/`` also moved ``tests/``. That
ratio is what this file exists to shrink, and it is expensive exactly once per
refactor, which is the kind of cost that gets re-litigated instead of paid down.

The underlying fact is that a test importing an ``app`` module's private name is
not making a choice -- it is reporting that the module has no level above its
insides, so there is nowhere else to stand. ``app/crawl/extraction.py`` published
four entry points that were all *compositions*, while the twenty contracts
underneath them were private; a caller reading the public surface would conclude
it was covered, and the suite knew better.

So the gate is deliberately blunt: **a private ``app`` name reaches the suite only
through a declared ledger row with a reason.** It is blunt because a gate that
weighs the merit of an import is a convention, and conventions decay. What the
bluntness buys is the only thing worth enforcing here -- a new weld cannot land
without somebody writing down why it is acceptable.

The ledger is :mod:`tests.private_import_ledger`, and it is expected to shrink.
Rows leave it as modules gain real surfaces (issues #70 through #82); rows that
stay get their kind and reason filled in, which turns a line item into a
decision. A row the suite no longer imports is a lie about the code, so the drift
guard below holds the names to reality in both directions and the hand-maintained
part is left as the reasons alone.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.chokepoint_walks import (
    REPO_ROOT,
    from_imports,
    nodes_of,
    relative_posix,
    resolved_expr_name,
    trees_under,
)
from tests.private_import_ledger import DECLARED as LEDGER
from tests.rules import Registry, Rule

_TESTS_DIR = REPO_ROOT / "tests"

# Conftest is where the harness reaches the process lifecycle it started, and it is
# the one file whose imports are plumbing rather than assertions. It is still gated
# -- an undeclared weld there is just as invisible -- but it is where the declared
# lifecycle rows legitimately live, so the name is worth naming in the failure text.
_LIFECYCLE_OWNER = "conftest"

# The three spellings by which the suite substitutes a private name. All three are in
# live use: ``monkeypatch.setattr`` (112 sites), ``unittest.mock.patch.object`` (3)
# and ``monkeypatch.delattr`` (1).
_PATCH_CALLS = frozenset({"setattr", "object", "delattr"})


def _private_app_names(tree: ast.Module) -> dict[str, int]:
    """``"module.name" -> lineno`` for every private ``app`` name the file reaches.

    Three forms reach the same private name and the first version of this gate read
    only one of them, which left 19 welds invisible: nineteen modules the suite
    reaches through a bound module object rather than a from-import, as in
    ``extraction_mod.quality_score``. A gate that can be walked around by
    spelling the import differently is not a gate, so every form is resolved to the
    same ``"module.name"`` key and the ledger holds names rather than spellings.

    The third form is the one the suite uses most and the gate could not see at all:
    ``monkeypatch.setattr(module, "_name", value)`` passes the reach as a string
    literal, so a detector reading imports and attribute accesses finds nothing.
    That is not a spelling detail -- it is 98 reaches, 45 of which had no ledger
    row while this gate reported the suite clean. The target is resolved through the
    module's own import bindings so the three spellings agree, and a target that
    resolves to a non-``app`` module is skipped: a test's own private double is the
    suite substituting something it owns, not reaching into ``app``.

    One blind spot survives and is not papered over: a name patched on an *object*
    the suite holds in a local (``monkeypatch.setattr(gate, "_usage_ratio", ...)``,
    18 sites) names no module, so no syntactic gate can resolve it. That reach is
    real and undeclared; closing it needs the factory's return type, not a wider
    spelling.
    """
    found: dict[str, int] = {}

    def record(key: str, lineno: int) -> None:
        module, _, name = key.rpartition(".")
        if module.startswith("app") and name.startswith("_") and not name.startswith("__"):
            found.setdefault(key, lineno)

    for node in from_imports(tree):
        if not node.module or not node.module.startswith("app") or node.level:
            continue
        for alias in node.names:
            record(f"{node.module}.{alias.name}", node.lineno)
    for node in nodes_of(tree):
        if not isinstance(node, ast.Attribute) or node.attr.startswith("__"):
            continue
        record(resolved_expr_name(tree, node), node.lineno)
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        callee = node.func
        spelling = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if spelling not in _PATCH_CALLS:
            continue
        literal = node.args[1]
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        target = resolved_expr_name(tree, node.args[0])
        if target:
            record(f"{target}.{literal.value}", node.lineno)
    return found


def _undeclared_private_import_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    where = " (harness owner)" if path.name == _LIFECYCLE_OWNER else ""
    return [
        f"{relative}{where}:{lineno}: {key} is a private app name with no ledger row"
        for key, lineno in sorted(_private_app_names(tree).items())
        if key not in LEDGER
    ]


PRIVATE_IMPORT_LEDGER = Rule(
    name="the-suite-imports-only-public-or-declared-app-names",
    find=_undeclared_private_import_offenders,
    summary=(
        "imports a private app name that the ledger does not declare; give the module a public "
        "surface, or record the name in tests/private_import_ledger.py with its kind and reason"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # An undeclared weld: the gate is the only thing standing between this and
        # a test whose import is invisible to a reviewer of the module it reaches.
        # The name has to be one the ledger does not already hold, or the proof
        # fires on nothing -- which is the failure this rule is meant to prevent.
        "from app.transport.browser import _undeclared_probe_name\n",
        # And the `import x.y` / `x.y._name` form, which reaches the same private
        # name through a module object instead of a from-import.
        "import app.crawl.engine\n\nassert app.crawl.engine._undeclared_probe\n",
        # And the string form, which is how the suite actually substitutes most of
        # what it reaches: the private name is a literal argument, so a detector
        # reading imports and attribute reads sees nothing at all.
        "import app.crawl.engine as engine\n\n"
        'monkeypatch.setattr(engine, "_undeclared_probe_name", lambda: None)\n',
    ),
    clean=lambda: (
        # A public name needs no ledger row -- that is the whole point of the fix.
        "from app.crawl.engine import crawl_request\n",
        # A declared lifecycle weld is allowed, and says why.
        "from app.crawl.extraction import shutdown_doc_extraction_executor\n",
        # A name from outside app/ is not this rule's business.
        "from tests.private_import_ledger import LEDGER\n",
        # A public name through a bound module object is the same name publicly,
        # so it needs no row either.
        "import app.crawl.extraction as extraction\n\nvalue = extraction.extract_content\n",
        # And the same exemption for the string form: a private name on a *test*
        # module, patched by string, is the suite substituting its own double.
        "import tests.membership_gate as gate\n\n"
        'monkeypatch.setattr(gate, "_local_probe_name", lambda: None)\n',
    ),
)

RULES = Registry((PRIVATE_IMPORT_LEDGER,))


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """A ledger gate that can only fire is indistinguishable from no gate.

    Both directions are proved: the bite is a private import nobody declared, and
    the clean set is a public name, a declared one, a non-``app`` module, and an
    attribute read. The last two matter because a rule keyed on the text
    ``_`` rather than on the import form would fire on all of them.
    """
    RULES.check_all_bite()


def test_ledger_rows_all_correspond_to_a_real_import():
    """A row the suite no longer imports is a lie about the code.

    This is what keeps the ledger from becoming a second hand-maintained mirror of
    the import graph: the names are verified in both directions on every run, so
    the only thing a maintainer can drift is a reason -- prose, which is theirs to
    hold. Without it, a row deleted from the suite would linger as a debt nobody
    is paying, and the ledger would grow a false floor.
    """
    actual: set[str] = set()
    for _, tree in trees_under(_TESTS_DIR):
        actual |= set(_private_app_names(tree))
    stale = sorted(key for key, dec in LEDGER.items() if "test" in dec.side and key not in actual)
    assert not stale, (
        f"{len(stale)} ledger row(s) name a private import the suite no longer makes; "
        f"delete them: {', '.join(stale)}"
    )


# ---------------------------------------------------------------------------
# app/ reaching into app/
# ---------------------------------------------------------------------------

_APP_DIR = REPO_ROOT / "app"


def _module_of(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _cross_module_private_names(tree: ast.Module, path: pathlib.Path) -> dict[str, int]:
    """``"module.name" -> lineno`` for private names reached across a module boundary.

    A module reaching its own privates is ordinary -- that is what private means --
    so the owning module has to be resolved and compared rather than assumed. The
    resolved name supplies both: ``from app.transport import body_decode``
    binds ``body_decode``, so ``body_decode.decode_body`` resolves to
    ``app.transport.body_decode.decode_body``, which names the module that
    owns the definition and not merely the binding the caller happens to use.
    """
    here = _module_of(path)
    found: dict[str, int] = {}
    for node in nodes_of(tree):
        if not isinstance(node, ast.Attribute) or node.attr.startswith("__"):
            continue
        key = resolved_expr_name(tree, node)
        if not key.startswith("app."):
            continue
        owner, _, attr = key.rpartition(".")
        if not owner.startswith("app") or "." not in owner or not attr.startswith("_"):
            continue
        if owner == here or owner == f"{here}.{attr}":
            continue
        found.setdefault(key, node.lineno)
    return found


def _cross_module_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    return [
        f"{relative_posix(path)}:{lineno}: {key} is private to another module"
        for key, lineno in sorted(_cross_module_private_names(tree, path).items())
        if key not in LEDGER
    ]


APP_PRIVATE_WELDS = Rule(
    name="app-modules-reach-each-other-through-public-names",
    find=_cross_module_private_offenders,
    summary=(
        "reaches into another module's private name; a sibling module is a caller, so the name is "
        "part of the seam between them and the module boundary is a fiction until it is public"
    ),
    bite=lambda: ("from app.transport import body_decode\n\ntext = body_decode._undeclared_probe\n",),
    clean=lambda: (
        # The public name for the same reach.
        "from app.transport import body_decode\n\ntext = body_decode.decode_body\n",
        # A module's own private name is what private means.
        "def helper():\n    return _internal_step()\n",
        # A declared lifecycle drain.
        "from app.transport import http_client_pool as pool\n\npool._drain_pending_client_closes()\n",
    ),
)

APP_RULES = Registry((APP_PRIVATE_WELDS,))


def test_app_has_no_cross_module_private_welds():
    APP_RULES.check_all_clean()


def test_app_weld_rule_bites():
    APP_RULES.check_all_bite()


def test_app_weld_rows_all_correspond_to_a_real_reach():
    """Same both-directions guard as the test-side ledger, for the same reason."""
    actual: set[str] = set()
    for path, tree in trees_under(_APP_DIR):
        actual |= set(_cross_module_private_names(tree, path))
    stale = sorted(key for key, dec in LEDGER.items() if "app" in dec.side and key not in actual)
    assert not stale, (
        f"{len(stale)} app weld row(s) name a reach that no longer happens; delete them: {', '.join(stale)}"
    )


# ---------------------------------------------------------------------------
# the harness reaches through a door, not through the state object
# ---------------------------------------------------------------------------

_CONFTEST = _TESTS_DIR / "conftest.py"


def _conftest_trees(*, exclude: frozenset[str] = frozenset()) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The one tree for ``tests/conftest.py``.

    ``trees_under`` rglobs, so handed a *file* it returns nothing at all -- an empty
    scope, which is the worst result available to a gate: green while inspecting zero
    files. That is not hypothetical. It is what the first version of this function
    did, and the reason the rule below carries a :attr:`~tests.rules.Rule.premise` is
    that an empty trigger set is now a construction error rather than a passing
    property.
    """
    del exclude
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"), filename=str(_CONFTEST))
    return ((_CONFTEST, tree),)


def _conftest_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every private ``app`` name ``conftest.py`` reaches, with nothing exempted.

    The general gate lets a declared row through. This one does not, and the
    difference is the argument rather than the strictness. In a test, a private
    reach says the module has no public door the test can stand on, so a declared
    row with a reason is a fair answer to that. In the autouse fixture it is not,
    because the fixture has no test to be near: it runs before every one of them,
    its imports are plumbing rather than assertions, and all of its reaches are the
    same obligation -- some module holds process-wide state, and the only way the
    harness can currently fulfil that is to reach in and mutate it.

    So a ledger row cannot rescue a reach here. A row would record "and here is
    why the harness mutates another module's private state", which is a true
    sentence and a useless one: every kind the ledger defines describes a reach
    that is *legitimate*, and none of them describes this.
    """
    return [
        f"{relative_posix(path)}:{lineno}: {key} is private to another module"
        for key, lineno in sorted(_private_app_names(tree).items())
    ]


CONFTEST_WELDS = Rule(
    name="the-harness-resets-module-state-through-a-public-door",
    find=_conftest_private_offenders,
    scope=_conftest_trees,
    premise=lambda: None if _conftest_trees() else pytest.fail("the conftest scope resolved to no trees"),
    summary=(
        "the autouse fixture reaches into a module to reset process-wide state; the owner should "
        "expose the reset, because a caller obliged to publish a reset has no other way to publish it"
    ),
    bite=lambda: (
        "from app.transport import pinned_transport as pinned_mod\n\n"
        "pinned_mod._undeclared_probe.clear()\n",
    ),
    clean=lambda: (
        # The shape this rule exists to force: ask the owner, do not touch the state.
        "from app.transport import pinned_transport\n\npinned_transport.reset_dns_cache()\n",
        # A public app name is the whole point.
        "from app.transport import pinned_transport\n\npinned_transport.apinned_hops\n",
        # Not an app import, so not this rule's business.
        "import os\n\nos.environ.get('CRAWLUTRON_LOG_LEVEL')\n",
    ),
)

CONFTEST_RULES = Registry((CONFTEST_WELDS,))


def test_conftest_reaches_no_undeclared_private_app_name():
    CONFTEST_RULES.check_all_clean()


def test_conftest_weld_rule_bites():
    CONFTEST_RULES.check_all_bite()
