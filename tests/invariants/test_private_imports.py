"""The suite reaches an ``app`` module through a public name, or through the ledger.

43 of the 49 structural commits that moved ``app/`` also moved ``tests/``; a
private import reports no public door, and only a declared row admits one."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

import pytest

from tests.chokepoint_walks import (
    REPO_ROOT,
    assignment_targets,
    from_imports,
    nodes_of,
    relative_posix,
    resolved_expr_name,
    trees_under,
)
from tests.private_import_ledger import DECLARED as LEDGER
from tests.rules import Registry, Rule

_TESTS_DIR = REPO_ROOT / "tests"

# Conftest is where the harness reaches the process lifecycle it started, and it is the one file whose imports
# are plumbing rather than assertions.
_LIFECYCLE_OWNER = "conftest"

# The three spellings by which the suite substitutes a private name. All three are in live use:
# ``monkeypatch.setattr`` (112 sites), ``unittest.mock.patch.object`` (3) and ``monkeypatch.delattr`` (1).
_PATCH_CALLS = frozenset({"setattr", "object", "delattr"})


def _app_class_instances(tree: ast.Module) -> dict[str, str]:
    """``local -> app class path`` for identifiers typed as an ``app`` class.

    An instance-private reach (``cache._index``) names no module, so only a
    base whose type names an ``app`` class can pin it to that class' home."""
    typed: dict[str, str] = {}
    for node in nodes_of(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation is not None and isinstance(arg.annotation, ast.Name):
                    home = resolved_expr_name(tree, arg.annotation)
                    if home.startswith("app."):
                        typed[arg.arg] = home
    for node in nodes_of(tree):
        if not isinstance(node, ast.AnnAssign) or isinstance(node.target, ast.Attribute):
            continue
        if isinstance(node.target, ast.Name) and isinstance(node.annotation, ast.Name):
            home = resolved_expr_name(tree, node.annotation)
            if home.startswith("app."):
                typed[node.target.id] = home
    for node in nodes_of(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        home = resolved_expr_name(tree, node.value.func)
        if not home.startswith("app."):
            continue
        # A call result is an app-class instance only when the callee is a class
        # name; PascalCase is the tree's class convention.
        if home.rsplit(".", 1)[-1][:1].islower():
            continue
        for target in assignment_targets(node):
            if isinstance(target, ast.Name):
                typed.setdefault(target.id, home)
    return typed


def _instance_private_key(tree: ast.Module, node: ast.Attribute, instances: dict[str, str]) -> str:
    """Class-qualified reach key for ``inst._private`` on an app-class instance.

    ``cache._index`` with ``cache: ContentCache`` is the same weld as
    ``ContentCache._index``, so the key must share the class' ledger row."""
    if not node.attr.startswith("_") or node.attr.startswith("__"):
        return ""
    if not isinstance(node.value, ast.Name):
        return ""
    home = instances.get(node.value.id)
    if not home:
        return ""
    return f"{home}.{node.attr}"


def _attribute_reach_keys(tree: ast.Module, instances: dict[str, str]) -> Iterator[tuple[str, int]]:
    """Resolved reach key and line for every private app attribute read.

    An instance base resolves to its class home via ``_instance_private_key``;
    any other base falls through to the import-resolved dotted name."""
    for node in nodes_of(tree):
        if not isinstance(node, ast.Attribute) or node.attr.startswith("__"):
            continue
        key = _instance_private_key(tree, node, instances) or resolved_expr_name(tree, node)
        if key:
            yield key, node.lineno


def _private_app_names(tree: ast.Module) -> dict[str, int]:
    """``"module.name" -> lineno`` for every private ``app`` name the file reaches.

    Import, bound module object, instance attribute and ``setattr`` (both
    orders) all resolve to one key; non-``app`` targets are skipped."""
    found: dict[str, int] = {}
    instances = _app_class_instances(tree)

    def record(key: str, lineno: int) -> None:
        module, _, name = key.rpartition(".")
        if module.startswith("app") and name.startswith("_") and not name.startswith("__"):
            found.setdefault(key, lineno)

    for node in from_imports(tree):
        if not node.module or not node.module.startswith("app") or node.level:
            continue
        for alias in node.names:
            record(f"{node.module}.{alias.name}", node.lineno)
    for key, lineno in _attribute_reach_keys(tree, instances):
        record(key, lineno)
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        callee = node.func
        spelling = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if spelling not in _PATCH_CALLS:
            continue
        target = resolved_expr_name(tree, node.args[0])
        literal = node.args[1]
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and target:
            record(f"{target}.{literal.value}", node.lineno)
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            record(first.value, node.lineno)
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
        # An undeclared weld: the gate is the only thing standing between this and a test whose import is
        # invisible to a reviewer of the module it reaches.
        "from app.transport.browser import _undeclared_probe_name\n",
        # And the `import x.y` / `x.y._name` form, which reaches the same private name through a module object
        # instead of a from-import.
        "import app.orchestration.engine\n\nassert app.orchestration.engine._undeclared_probe\n",
        # And the string form, which is how the suite actually substitutes most of what it reaches: the
        # private name is a literal argument, so a detector reading imports and attribute reads sees nothing
        "import app.orchestration.engine as engine\n\n"
        'monkeypatch.setattr(engine, "_undeclared_probe_name", lambda: None)\n',
        # An instance private: `cache._index` has no module in its own spelling, so only the base's type
        # (an app class) can pin the reach to the class that owns the member.
        "from app.transport.content_cache import ContentCache\n\n"
        'cache = ContentCache("/tmp/x")\n\nassert cache._undeclared_probe_index\n',
        # And the string-FIRST form, where the target is the first argument, not the replacement.
        'import pytest\n\nmonkeypatch.setattr("app.api.job_runner._undeclared_probe", lambda: None)\n',
    ),
    clean=lambda: (
        # A public name needs no ledger row -- that is the whole point of the fix.
        "from app.orchestration.engine import crawl_request\n",
        # A declared lifecycle weld is allowed, and says why.
        "from app.extraction.content import shutdown_doc_extraction_executor\n",
        # A name from outside app/ is not this rule's business.
        "from tests.private_import_ledger import LEDGER\n",
        # A public name through a bound module object is the same name publicly, so it needs no row either.
        "import app.extraction.content as extraction\n\nvalue = extraction.extract_content\n",
        # And the same exemption for the string form: a private name on a *test* module, patched by string, is
        # the suite substituting its own double.
        "import tests.membership_gate as gate\n\n"
        'monkeypatch.setattr(gate, "_local_probe_name", lambda: None)\n',
        # A PUBLIC member on an app-class instance is not a weld.
        "from app.transport.content_cache import ContentCache\n\n"
        'cache = ContentCache("/tmp/x")\n\nassert cache.cache_dir\n',
        # A private member on an unrelated local object is not an app reach.
        "class _LocalProbe:\n    def __init__(self):\n        self._probe = 1\n\n"
        "p = _LocalProbe()\n\nassert p._probe\n",
        # A string-first setattr of a PUBLIC app name still needs no row.
        'monkeypatch.setattr("app.api.job_runner.run_job", fake)\n',
    ),
)

RULES = Registry((PRIVATE_IMPORT_LEDGER,))


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """A ledger gate that can only fire is indistinguishable from no gate.

    Both directions proved: an undeclared private import bites; a public name,
    a declared row and a non-``app`` module stay clean."""
    RULES.check_all_bite()


def test_ledger_rows_all_correspond_to_a_real_import():
    """A row the suite no longer imports is a lie about the code.

    Names are verified both ways every run, so only a reason can drift;
    otherwise a deleted row lingers as debt and the ledger grows a false floor."""
    actual: set[str] = set()
    for _, tree in trees_under(_TESTS_DIR):
        actual |= set(_private_app_names(tree))
    stale = sorted(key for key, dec in LEDGER.items() if "test" in dec.side and key not in actual)
    assert not stale, (
        f"{len(stale)} ledger row(s) name a private import the suite no longer makes; "
        f"delete them: {', '.join(stale)}"
    )


# --------------------------------------------------------------------------- app/ reaching into app/
# ---------------------------------------------------------------------------

_APP_DIR = REPO_ROOT / "app"


def _module_of(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)


def _cross_module_private_names(tree: ast.Module, path: pathlib.Path) -> dict[str, int]:
    """``"module.name" -> lineno`` for private names reached across a module boundary.

    ``body_decode.decode_body`` names the owner module, not the bind; a private
    from-import (``from app.other import _priv``) is the same weld as a read."""
    here = _module_of(path)
    found: dict[str, int] = {}
    for node in from_imports(tree):
        if not node.module or not node.module.startswith("app") or node.level:
            continue
        owner, _, attr = node.module.rpartition(".")
        if owner == here or node.module == here:
            continue
        for alias in node.names:
            if alias.name.startswith("_") and not alias.name.startswith("__"):
                found.setdefault(f"{node.module}.{alias.name}", node.lineno)
    for key, lineno in _attribute_reach_keys(tree, {}):
        if not key.startswith("app."):
            continue
        owner, _, attr = key.rpartition(".")
        if not owner.startswith("app") or "." not in owner or not attr.startswith("_"):
            continue
        if owner == here or owner == f"{here}.{attr}":
            continue
        found.setdefault(key, lineno)
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
    bite=lambda: (
        "from app.transport import body_decode\n\ntext = body_decode._undeclared_probe\n",
        # The from-import spelling of the same weld: `from app.other import _priv` in an app
        # module is a cross-module reach an attribute-only walk never sees.
        "from app.transport.http_cache import _undeclared_fromimport_probe\n",
        # A private TYPE from-import is the same removable seam.
        "from app.orchestration.request_pipeline import _UndeclaredPipelineState\n",
    ),
    clean=lambda: (
        # The public name for the same reach.
        "from app.transport import body_decode\n\ntext = body_decode.decode_body\n",
        # A module's own private name is what private means.
        "def helper():\n    return _internal_step()\n",
        # A declared lifecycle drain.
        "from app.transport import pooled_clients as pool\n\npool._drain_pending_client_closes()\n",
        # A from-import of a PUBLIC name is not a weld.
        "from app.transport.http_cache import is_expired\n",
    ),
)

APP_RULES = Registry((APP_PRIVATE_WELDS,))


_SCRIPTS_DIR = REPO_ROOT / "scripts"


def _scripts_private_import_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every private ``app`` name a ``scripts`` module reaches, unledgered.

    Same rule of thumb as the test tree: a helper reaching into an ``app``
    private is a weld that needs declaring, and scripts/ was outside this gate."""
    return [
        f"{relative_posix(path)}:{lineno}: {key} is a private app name with no ledger row"
        for key, lineno in sorted(_private_app_names(tree).items())
        if key not in LEDGER
    ]


SCRIPTS_PRIVATE_IMPORTS = Rule(
    name="scripts-reach-app-through-public-or-declared-names",
    find=_scripts_private_import_offenders,
    summary=(
        "a scripts/ module reaches an app private name the ledger does not declare; "
        "give the app module a public door or record the name in the private_import_ledger"
    ),
    scope=lambda *, exclude: trees_under(_SCRIPTS_DIR),
    bite=lambda: (
        "from app.safety.validation import _undeclared_script_probe\n",
        "import app.orchestration.engine as engine\n\ncall(engine._private_lift)\n",
    ),
    clean=lambda: (
        # A public app door needs no ledger row.
        "from app.transport.browser_driver import BrowserDriver\n",
        # A non-app import is not this rule's business.
        "import pathlib\n\nroot = pathlib.Path('.')\n",
    ),
)

SCRIPTS_RULES = Registry((SCRIPTS_PRIVATE_IMPORTS,))


def test_app_has_no_cross_module_private_welds():
    APP_RULES.check_all_clean()


def test_scripts_reach_no_undeclared_private_app_name():
    SCRIPTS_RULES.check_all_clean()


def test_scripts_weld_rule_bites():
    """The H1 reach: a scripts/ private app import fires instead of passing silently.
    scripts/ was outside every private-import rule; a helper importing an app
    private went unledgered. The bite proves the rule now sweeps the scripts tree."""
    SCRIPTS_RULES.check_all_bite()


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


# --------------------------------------------------------------------------- the harness reaches through a
# door, not through the state object

_CONFTEST = _TESTS_DIR / "conftest.py"


def _conftest_trees(*, exclude: frozenset[str] = frozenset()) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The one tree for ``tests/conftest.py``.

    ``trees_under`` rglobs, so handed a FILE it returns nothing -- green while
    inspecting zero files: the empty-trigger construction error."""
    del exclude
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"), filename=str(_CONFTEST))
    return ((_CONFTEST, tree),)


def _conftest_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every private ``app`` name ``conftest.py`` reaches, with nothing exempted.

    The autouse fixture has no test to be near -- its reaches are plumbing,
    one obligation (mutating process-wide state), no ledger kind describes it."""
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
        "from app.transport import pinned_transport as pinned_mod\n\npinned_mod._undeclared_probe.clear()\n",
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
