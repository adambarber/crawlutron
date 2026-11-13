"""The suite reaches a ``crawlutron`` module only through a public name.

A private name is the owner's seam; a test that needs a name the module does not
make public is asking the module to widen its promise, not a reach to excuse."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

import pytest

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    assignment_targets,
    from_imports,
    nodes_of,
    parsed,
    relative_posix,
    resolved_expr_name,
    trees_under,
)
from tests.invariants._shared import _private_from_import_names, module_level_bindings
from tests.rules import Bites, Registry, Rule

_TESTS_DIR = REPO_ROOT / "tests"

# Conftest is where the harness reaches the process lifecycle it started, and it is the one file whose imports
# are plumbing rather than assertions.
_LIFECYCLE_OWNER = "conftest"

# The three spellings by which the suite substitutes a private name. All three are in live use:
# ``monkeypatch.setattr`` (112 sites), ``unittest.mock.patch.object`` (3) and ``monkeypatch.delattr`` (1).
_PATCH_CALLS = frozenset({"setattr", "object", "delattr"})


def _app_class_instances(tree: ast.Module) -> dict[str, str]:
    """``local -> app class path`` for identifiers typed as a ``crawlutron`` class.

    An instance-private reach (``cache._index``) names no module, so only a
    base whose type names a ``crawlutron`` class can pin it to that class' home."""
    typed: dict[str, str] = {}
    for node in nodes_of(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation is not None and isinstance(arg.annotation, ast.Name):
                    home = resolved_expr_name(tree, arg.annotation)
                    if home.startswith("crawlutron."):
                        typed[arg.arg] = home
    for node in nodes_of(tree):
        if not isinstance(node, ast.AnnAssign) or isinstance(node.target, ast.Attribute):
            continue
        if isinstance(node.target, ast.Name) and isinstance(node.annotation, ast.Name):
            home = resolved_expr_name(tree, node.annotation)
            if home.startswith("crawlutron."):
                typed[node.target.id] = home
    for node in nodes_of(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        home = resolved_expr_name(tree, node.value.func)
        if not home.startswith("crawlutron."):
            continue
        # A call result is an crawlutron-class instance only when the callee is a class
        # name; PascalCase is the tree's class convention.
        if home.rsplit(".", 1)[-1][:1].islower():
            continue
        for target in assignment_targets(node):
            if isinstance(target, ast.Name):
                typed.setdefault(target.id, home)
    return typed


def _instance_private_key(tree: ast.Module, node: ast.Attribute, instances: dict[str, str]) -> str:
    """Class-qualified reach key for ``inst._private`` on an crawlutron-class instance.

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
    """Resolved reach key and line for every private crawlutron attribute read.

    An instance base resolves to its class home via ``_instance_private_key``;
    any other base falls through to the import-resolved dotted name."""
    for node in nodes_of(tree):
        if not isinstance(node, ast.Attribute) or node.attr.startswith("__"):
            continue
        key = _instance_private_key(tree, node, instances) or resolved_expr_name(tree, node)
        if key:
            yield key, node.lineno


def _private_app_names(tree: ast.Module) -> dict[str, int]:
    """``"module.name" -> lineno`` for every private ``crawlutron`` name the file reaches.

    Import, bound module object, instance attribute and ``setattr`` (both
    orders) all resolve to one key; non-``crawlutron`` targets are skipped."""
    found: dict[str, int] = {}
    instances = _app_class_instances(tree)

    def record(key: str, lineno: int) -> None:
        module, _, name = key.rpartition(".")
        if module.startswith("crawlutron") and name.startswith("_") and not name.startswith("__"):
            found.setdefault(key, lineno)

    from_nodes: tuple[ast.ImportFrom, ...] = from_imports(tree)
    for import_node in from_nodes:
        if not import_node.module or not import_node.module.startswith("crawlutron") or import_node.level:
            continue
        for alias in import_node.names:
            record(f"{import_node.module}.{alias.name}", import_node.lineno)
    for key, lineno in _attribute_reach_keys(tree, instances):
        record(key, lineno)
    reached: tuple[ast.AST, ...] = nodes_of(tree)
    for node in reached:
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
        f"{relative}{where}:{lineno}: {key} is a private app name with no public door"
        for key, lineno in sorted(_private_app_names(tree).items())
    ]


PRIVATE_IMPORT_LEDGER = Rule(
    name="the-suite-imports-only-public-app-names",
    find=_undeclared_private_import_offenders,
    summary=(
        "imports a private app name; a private name is the owner's seam, so the test needs a "
        "public door through the module's own surface"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    skip=frozenset(["tests/api/test_caller_concurrency.py"]),
    bite=lambda: (
        # An undeclared weld: the gate is the only thing standing between this and a test whose import is
        # invisible to a reviewer of the module it reaches.
        "from crawlutron.transport.browser import _undeclared_probe_name\n",
        # And the `import x.y` / `x.y._name` form, which reaches the same private name through a module object
        # instead of a from-import.
        "import crawlutron.orchestration.engine\nassert crawlutron.orchestration.engine._undeclared_probe\n",
        # And the string form, which is how the suite actually substitutes most of what it reaches: the
        # private name is a literal argument, so a detector reading imports and attribute reads sees nothing
        "import crawlutron.orchestration.engine as engine\n\n"
        'monkeypatch.setattr(engine, "_undeclared_probe_name", lambda: None)\n',
        # An instance private: `cache._index` has no module in its own spelling, so only the base's type
        # (an app class) can pin the reach to the class that owns the member.
        "from crawlutron.transport.content_cache import ContentCache\n\n"
        'cache = ContentCache("/tmp/x")\n\nassert cache._undeclared_probe_index\n',
        # And the string-FIRST form, where the target is the first argument, not the replacement.
        "import pytest\n\n"
        'monkeypatch.setattr("crawlutron.service.job_runner._undeclared_probe", lambda: None)\n',
    ),
    clean=lambda: (
        # A public name is the whole point -- it carries no exemption.
        "from crawlutron.orchestration.engine import crawl_request\n",
        # The doc-extraction executor's public teardown is callable.
        "from crawlutron.extraction.content import shutdown_doc_extraction_executor\n",
        # A name from outside app/ is not this rule's business.
        "from crawlutron.models.crawl_result import EngineCrawlResult\n",
        # A public name through a bound module object is the same name publicly, so it needs no row either.
        "import crawlutron.extraction.content as extraction\n\nvalue = extraction.extract_content\n",
        # And the same exemption for the string form: a private name on a *test* module, patched by string, is
        # the suite substituting its own double.
        "import tests.membership_gate as gate\n\n"
        'monkeypatch.setattr(gate, "_local_probe_name", lambda: None)\n',
        # A PUBLIC member on an crawlutron-class instance is not a weld.
        "from crawlutron.transport.content_cache import ContentCache\n\n"
        'cache = ContentCache("/tmp/x")\n\nassert cache.cache_dir\n',
        # A private member on an unrelated local object is not an app reach.
        "class _LocalProbe:\n    def __init__(self):\n        self._probe = 1\n\n"
        "p = _LocalProbe()\n\nassert p._probe\n",
        # A string-first setattr of a PUBLIC app name still needs no row.
        'monkeypatch.setattr("crawlutron.service.job_runner.run_job", fake)\n',
    ),
)

RULES = Registry((PRIVATE_IMPORT_LEDGER,))


def test_no_rule_violations():
    RULES.check_all_clean()


def _module_of(path: pathlib.Path) -> str:
    """A module's import name from its repo path, past the workspace member prefix."""
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    try:
        start = parts.index("crawlutron")
    except ValueError:
        return ".".join(parts)
    return ".".join(parts[start:])


def _cross_module_private_names(tree: ast.Module, path: pathlib.Path) -> dict[str, int]:
    """``"module.name" -> lineno`` for private names reached across a module boundary.

    ``body_decode.decode_body`` names the owner module, not the bind; a private
    from-import (``from crawlutron.other import _priv``) is the same weld as a read."""
    here = _module_of(path)
    found: dict[str, int] = _private_from_import_names(
        tree, lambda module: module.startswith("crawlutron"), here
    )
    for key, lineno in _attribute_reach_keys(tree, {}):
        if not key.startswith("crawlutron."):
            continue
        owner, _, attr = key.rpartition(".")
        if not owner.startswith("crawlutron") or "." not in owner or not attr.startswith("_"):
            continue
        if owner == here or owner == f"{here}.{attr}":
            continue
        found.setdefault(key, lineno)
    return found


def _cross_module_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    return [
        f"{relative_posix(path)}:{lineno}: {key} is private to another module"
        for key, lineno in sorted(_cross_module_private_names(tree, path).items())
    ]


APP_PRIVATE_WELDS = Rule(
    name="app-modules-reach-each-other-through-public-names",
    find=_cross_module_private_offenders,
    summary=(
        "reaches into another module's private name; a sibling module is a caller, so the name is "
        "part of the seam between them and the module boundary is a fiction until it is public"
    ),
    bite=lambda: (
        "from crawlutron.transport import body_decode\n\ntext = body_decode._undeclared_probe\n",
        # The from-import spelling of the same weld: `from crawlutron.other import _priv` in an app
        # module is a cross-module reach an attribute-only walk never sees.
        "from crawlutron.transport.http_cache import _undeclared_fromimport_probe\n",
        # A private TYPE from-import is the same removable seam.
        "from crawlutron.orchestration.request_pipeline import _UndeclaredPipelineState\n",
    ),
    clean=lambda: (
        # The public name for the same reach.
        "from crawlutron.transport import body_decode\n\ntext = body_decode.decode_body\n",
        # A module's own private name is what private means.
        "def helper():\n    return _internal_step()\n",
        # A cross-module PUBLIC call (drain is this module's public door).
        "from crawlutron.transport import pooled_clients as pool\n\npool.drain_pending_client_closes()\n",
        # A from-import of a PUBLIC name is not a weld.
        "from crawlutron.transport.http_cache import is_expired\n",
    ),
)

APP_RULES = Registry((APP_PRIVATE_WELDS,))


_SCRIPTS_DIR = REPO_ROOT / "scripts"


def _scripts_private_import_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every private ``crawlutron`` name a ``scripts`` module reaches.

    Same rule of thumb as the test tree: a helper reaching into an ``app``
    private is a weld that needs a public door, and scripts/ was outside this gate."""
    return [
        f"{relative_posix(path)}:{lineno}: {key} is a private app name with no public door"
        for key, lineno in sorted(_private_app_names(tree).items())
    ]


SCRIPTS_PRIVATE_IMPORTS = Rule(
    name="scripts-reach-app-through-public-names",
    find=_scripts_private_import_offenders,
    summary=(
        "a scripts/ module reaches an app private name; the app module should expose "
        "a public door for a caller outside its package"
    ),
    scope=lambda *, exclude: trees_under(_SCRIPTS_DIR),
    bite=lambda: (
        "from crawlutron.safety.validation import _undeclared_script_probe\n",
        "import crawlutron.orchestration.engine as engine\n\ncall(engine._private_lift)\n",
    ),
    clean=lambda: (
        # A public app door needs no ledger row.
        "from crawlutron.transport.browser_driver import BrowserDriver\n",
        # A non-app import is not this rule's business.
        "import pathlib\n\nroot = pathlib.Path('.')\n",
    ),
)

SCRIPTS_RULES = Registry((SCRIPTS_PRIVATE_IMPORTS,))


# ----------------------------------------------------------------------- a public name is imported from its
# owner (definition site or declared __all__ re-export)

_OWNER_PUBLIC: dict[str, frozenset[str]] | None = None


def _declared_all(tree: ast.Module) -> set[str]:
    """The module's own ``__all__`` entries: its declared public doors."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                continue
        elif isinstance(node, ast.AnnAssign):
            if not (isinstance(node.target, ast.Name) and node.target.id == "__all__"):
                continue
        else:
            continue
        return {str(e.value) for e in getattr(node.value, "elts", ()) if isinstance(e, ast.Constant)}
    return set()


def _module_public_names(tree: ast.Module) -> frozenset[str]:
    """Names a module itself publishes: top-level bindings plus ``__all__``."""
    return frozenset(module_level_bindings(tree) | _declared_all(tree))


def _owner_public_names() -> dict[str, frozenset[str]]:
    """``module -> public names`` for every crawlutron module, parsed once per process."""
    global _OWNER_PUBLIC
    if _OWNER_PUBLIC is None:
        _OWNER_PUBLIC = {_module_of(path): _module_public_names(tree) for path, tree in trees_under(APP_ROOT)}
    return _OWNER_PUBLIC


def _non_owner_public_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Public crawlutron names this module from-imports from a non-owner module.

    A name imports from its definition site or a ``__all__``-declared re-export;
    anything else is an undeclared pass-through. Private names are the rule above."""
    owners = _owner_public_names()
    here = _module_of(path)
    found: list[str] = []
    app_imports = (
        node
        for node in from_imports(tree)
        if node.module and node.module.startswith("crawlutron") and not node.level
    )
    for node in app_imports:
        assert node.module is not None  # the generator above already filters on it
        owner, _, _leaf = node.module.rpartition(".")
        if owner == here or node.module == here:
            continue
        defined = owners.get(node.module)
        if defined is None:
            continue
        for alias in node.names:
            if alias.name.startswith("_"):
                continue
            if f"{node.module}.{alias.name}" in owners:
                continue
            if alias.name in defined:
                continue
            found.append(
                f"{relative_posix(path)}:{node.lineno}: {node.module}.{alias.name} is not "
                f"{node.module}'s to give -- import it from the module that defines it"
            )
    return found


PUBLIC_OWNER_WELDS = Rule(
    name="app-imports-public-names-from-their-owners",
    find=_non_owner_public_imports,
    summary=(
        "imports a public app name from a module that neither defines it nor declares "
        "the re-export in __all__; import the name from its definition site -- an "
        "undeclared pass-through is a second home for one declaration"
    ),
    bite=lambda: (
        Bites("from crawlutron.safety.validation import HostPolicy\n", "HostPolicy"),
        Bites("from crawlutron.admission.admission import job_max_pages\n", "job_max_pages"),
    ),
    clean=lambda: (
        # The definition site is the whole point.
        "from safe_url.host_policy import HostPolicy\n",
        # A declared __all__ re-export is a public door.
        "from crawlutron.storage.job_store import job_max_pages\n",
        "from crawlutron.transport.http_client_pool import get_async_http_client\n",
        # A submodule import names a module, not a name.
        "from crawlutron.transport import content_cache\n",
        # Relative imports are intra-package.
        "from .job_store_collections import TERMINAL_STATUSES\n",
    ),
)


def test_app_imports_public_names_from_their_owners():
    Registry((PUBLIC_OWNER_WELDS,)).check_all_clean()


def test_annotated_all_declares_public_doors() -> None:
    """A name exported only through an annotated __all__ is still a module's public door."""
    source = "from crawlutron.other import helper\n\n__all__: tuple[str, ...] = ('helper',)\n"
    assert "helper" in _module_public_names(ast.parse(source))


def test_annotated_all_literal_shapes_yield_their_doors() -> None:
    """tuple/list/set literals in an annotated __all__ each declare their doors."""
    for literal in ('("helper",)', '["helper"]', '{"helper"}'):
        source = f"__all__: list[object] = {literal}\n"
        assert _declared_all(ast.parse(source)) == {"helper"}


def test_annotated_all_without_value_yields_no_doors() -> None:
    """__all__ with only an annotation (no value) must not crash and declare no doors."""
    assert _declared_all(ast.parse("__all__: list[str]\n")) == set()


def test_non_all_annassign_is_not_picked_up() -> None:
    """An annotated assignment to a non-__all__ name is not a door declaration."""
    assert _declared_all(ast.parse('helper: str = "x"\n')) == set()


def test_app_has_no_cross_module_private_welds():
    APP_RULES.check_all_clean()


def test_scripts_reach_no_undeclared_private_app_name():
    SCRIPTS_RULES.check_all_clean()


# --------------------------------------------------------------------------- the harness reaches through a
# door, not through the state object

_CONFTEST = _TESTS_DIR / "conftest.py"


def _conftest_trees(*, exclude: frozenset[str] = frozenset()) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The one tree for ``tests/conftest.py``.

    ``trees_under`` rglobs, so handed a FILE it returns nothing -- green while
    inspecting zero files: the empty-trigger construction error."""
    del exclude
    tree = parsed(_CONFTEST)
    return ((_CONFTEST, tree),)


def _conftest_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every private ``crawlutron`` name ``conftest.py`` reaches, with nothing exempted.

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
        "from crawlutron.transport import pinned_transport as pinned_mod\n"
        "pinned_mod._undeclared_probe.clear()\n",
    ),
    clean=lambda: (
        # The shape this rule exists to force: ask the owner, do not touch the state.
        "from crawlutron.transport import pinned_transport\n\npinned_transport.reset_dns_cache()\n",
        # A public app name is the whole point.
        "from crawlutron.transport import pinned_transport\n\npinned_transport.build_pinned_request\n",
        # Not an app import, so not this rule's business.
        "import os\n\nos.environ.get('CRAWLUTRON_LOG_LEVEL')\n",
    ),
)

CONFTEST_RULES = Registry((CONFTEST_WELDS,))


def test_conftest_reaches_no_undeclared_private_app_name():
    CONFTEST_RULES.check_all_clean()
