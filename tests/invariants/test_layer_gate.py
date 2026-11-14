"""Layer gate: module-boundary and import-hygiene rules from the rule table.

Walks the build_graph surface and names every offender at file:line.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable

from scripts.module_graph import IMPORT_ROOTS, _runtime_imports, build_graph
from tests._walk_primitives import REPO_ROOT, offender_at
from tests._walk_readers import call_nodes
from tests.chokepoint_walks import trees_under
from tests.rules import Bites, Rule

#: Enforce the vacuity kill-switch: build_graph raises when it walks nothing.
build_graph()

#: Every walked module as (path, tree): the mechanical scope, never hand-typed.
_WALKED: tuple[tuple[pathlib.Path, ast.Module], ...] = tuple(
    (path, tree) for import_dir, _name in IMPORT_ROOTS for path, tree in trees_under(import_dir)
)

#: models/vocabulary subtrees for the upward-import rule.
_MODELS_DIR = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "models"
_VOCAB_DIR = REPO_ROOT / "packages" / "core" / "src" / "crawlutron" / "vocabulary"
_MODELS_VOCAB_TREES: tuple[tuple[pathlib.Path, ast.Module], ...] = tuple(
    (path, tree) for directory in (_MODELS_DIR, _VOCAB_DIR) for path, tree in trees_under(directory)
)

#: Package roots for the layer rules, derived from the walked surface, never hand-typed.
_REST_PREFIX = "packages/rest/src/crawlutron_rest/"
_FETCHERS = "packages/core/src/crawlutron/transport/fetchers.py"
_POOL = "packages/core/src/crawlutron/runtime/pool.py"
_IDS = "packages/core/src/crawlutron/storage/ids.py"
_SANCTIONED_SUBPROCESS = "packages/rest/src/crawlutron_rest/cli/docker.py"
_ALLOWED_SETTINGS = (
    "packages/core/src/crawlutron/service/",
    "packages/core/src/crawlutron/orchestration/",
    "packages/rest/src/crawlutron_rest/",
    "packages/mcp/src/crawlutron_mcp/",
)
_WRITE_MODES = ("w", "a", "x", "r+", "w+", "a+", "x+")

_SKIP_FASTAPI = frozenset({"packages/core/src/crawlutron/health/readiness.py"})
_SKIP_EGRESS = frozenset(
    {
        "packages/core/src/crawlutron/search/providers.py",
        "packages/core/src/crawlutron/service/webhooks.py",
        "packages/core/src/crawlutron/transport/classification.py",
        "packages/core/src/crawlutron/transport/cffi_transport.py",
        "packages/core/src/crawlutron/transport/pinned_transport.py",
        "packages/core/src/crawlutron/transport/pooled_clients.py",
        "packages/core/src/crawlutron/transport/pooled_cffi.py",
        "packages/core/src/crawlutron/transport/timeouts.py",
        "packages/rest/src/crawlutron_rest/cli/__main__.py",
        "packages/rest/src/crawlutron_rest/cli/client.py",
        "packages/sdk/src/crawlutron_sdk/client.py",
    }
)
_SKIP_SETTINGS = frozenset(
    {
        "packages/core/src/crawlutron/extraction/documents.py",
        "packages/core/src/crawlutron/search/providers_api.py",
        "packages/core/src/crawlutron/transport/browser.py",
        "packages/core/src/crawlutron/transport/browser_pool.py",
        "packages/core/src/crawlutron/transport/content_cache.py",
        "packages/core/src/crawlutron/transport/content_cache_key.py",
        "packages/core/src/crawlutron/transport/content_pipeline.py",
        "packages/core/src/crawlutron/transport/http.py",
        "packages/core/src/crawlutron/transport/http_response.py",
        "packages/core/src/crawlutron/transport/pooled_clients.py",
    }
)
_SKIP_EXECUTOR = frozenset(
    {
        "packages/core/src/crawlutron/extraction/documents.py",
        "packages/core/src/crawlutron/storage/coordination_store.py",
        "packages/core/src/crawlutron/transport/document.py",
        "packages/core/src/crawlutron/transport/pinned_dns.py",
        "packages/rest/src/crawlutron_rest/main.py",
    }
)

#: crawlutron.models/vocabulary modules that import a higher layer at runtime.
#: Each entry names an accepted upward edge; the set shrinks one module per arc.
_SKIP_UPWARD = frozenset(
    {
        "packages/core/src/crawlutron/models/caller_identity.py",
        "packages/core/src/crawlutron/models/crawl_policy.py",
        "packages/core/src/crawlutron/models/crawl_policy_vocabulary.py",
        "packages/core/src/crawlutron/models/crawl_request.py",
        "packages/core/src/crawlutron/models/credential.py",
        "packages/core/src/crawlutron/models/fetch_validators.py",
    }
)

#: Module-level mutable state holders not yet registered with process_state.
#: Each should register a reset/probe pair; the set shrinks as owners comply.
_SKIP_MUTABLE = frozenset(
    {
        "packages/core/src/crawlutron/extraction/llm_format.py",
        "packages/core/src/crawlutron/transport/robots.py",
    }
)


def _scope(*, exclude: frozenset[str] = frozenset()) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """Every walked module; ``exclude`` is honored by the Rule's violation filter."""
    _ = exclude
    return _WALKED


def _models_vocab_scope(
    *, exclude: frozenset[str] = frozenset()
) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """models/vocabulary trees; ``exclude`` is honored by the Rule's violation filter."""
    _ = exclude
    return _MODELS_VOCAB_TREES


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _import_node(tree: ast.AST, wanted: str) -> ast.Import | ast.ImportFrom | None:
    for node in _runtime_imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == wanted or alias.name.startswith(wanted + "."):
                    return node
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == wanted or node.module.startswith(wanted + "."):
                return node
            for alias in node.names:
                if alias.name == wanted:
                    return node
    return None


def _first_match(tree: ast.AST, predicate: Callable[[ast.AST], bool]) -> ast.AST | None:
    for node in ast.walk(tree):
        if predicate(node):
            return node
    return None


def _call_mode(call: ast.Call) -> str | None:
    if len(call.args) >= 2:
        arg = call.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    for keyword in call.keywords:
        if (
            keyword.arg == "mode"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _upward_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """crawlutron.models/vocabulary importing a higher layer."""
    out: list[str] = []
    for node in _runtime_imports(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("crawlutron."):
            layer = node.module.split(".")[1]
            if layer not in ("models", "vocabulary"):
                out.append(offender_at(path, node, f"{node.module} imported above models/vocabulary"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("crawlutron."):
                    layer = alias.name.split(".")[1]
                    if layer not in ("models", "vocabulary"):
                        out.append(offender_at(path, node, f"{alias.name} imported above models/vocabulary"))
    return out


def _fastapi_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """fastapi imported outside packages/rest."""
    out: list[str] = []
    node = _import_node(tree, "fastapi")
    if node is None or _rel(path).startswith(_REST_PREFIX):
        return out
    return [offender_at(path, node, "fastapi imported outside packages/rest")]


def _egress_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """httpx or curl_cffi imported outside the egress module."""
    out: list[str] = []
    if _rel(path) == _FETCHERS:
        return out
    for wanted in ("httpx", "curl_cffi"):
        node = _import_node(tree, wanted)
        if node is not None:
            out.append(offender_at(path, node, f"{wanted} imported outside the egress module"))
    return out


def _settings_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """crawlutron.config.settings imported below the service layer."""
    out: list[str] = []
    node = _import_node(tree, "crawlutron.config.settings")
    if node is None:
        return out
    if any(_rel(path).startswith(prefix) for prefix in _ALLOWED_SETTINGS):
        return out
    return [offender_at(path, node, "crawlutron.config.settings imported below the service layer")]


def _uuid4_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """uuid.uuid4() outside storage/ids.py."""
    out: list[str] = []
    if _rel(path) == _IDS:
        return out
    for call in call_nodes(tree):
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "uuid4"
            and isinstance(func.value, ast.Name)
            and func.value.id == "uuid"
        ):
            out.append(offender_at(path, call, "uuid.uuid4() outside storage/ids.py"))
    return out


def _open_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """write-mode open() outside safety.atomic_write."""
    out: list[str] = []
    for call in call_nodes(tree):
        func = call.func
        if not (isinstance(func, ast.Name) and func.id == "open"):
            continue
        mode = _call_mode(call)
        if mode is None or not any(mode.startswith(wm) for wm in _WRITE_MODES):
            continue
        out.append(offender_at(path, call, "write-mode open() outside safety.atomic_write"))
    return out


def _executor_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """ThreadPoolExecutor outside runtime/pool.py."""
    out: list[str] = []
    node: ast.AST | None = _import_node(tree, "ThreadPoolExecutor")
    if node is None:
        node = _first_match(
            tree,
            lambda n: (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "ThreadPoolExecutor"
            ),
        )
    if node is None or _rel(path) == _POOL:
        return out
    return [offender_at(path, node, "ThreadPoolExecutor outside runtime/pool.py")]


def _subprocess_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """subprocess imported outside sanctioned sites."""
    out: list[str] = []
    node = _import_node(tree, "subprocess")
    if node is None or _rel(path) == _SANCTIONED_SUBPROCESS:
        return out
    return [offender_at(path, node, "subprocess outside sanctioned sites")]


def _is_empty_literal(value: ast.expr) -> bool:
    """An empty ``[]``/``{}``/``set()`` literal -- a fresh accumulator at module scope."""
    if isinstance(value, (ast.List, ast.Set)):
        return len(value.elts) == 0
    if isinstance(value, ast.Dict):
        return len(value.keys) == 0
    return False


def _mutable_containers(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """module-level empty mutable container not registered with process_state.

    Populated literals (``_CSV_COLUMNS = [...]``) are constant tables, not state;
    only empty accumulators (``_inflight: dict = {}``) hide mutable global state.
    """
    out: list[str] = []
    if _rel(path).endswith("process_state.py"):
        return out
    if _registers_process_state(tree):
        return out
    for stmt in tree.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        name = _assign_name(stmt)
        if name is None or name == "__all__" or (name.startswith("__") and name.endswith("__")):
            continue
        value = stmt.value
        if isinstance(value, (ast.List, ast.Dict, ast.Set)) and _is_empty_literal(value):
            out.append(
                offender_at(path, stmt, "module-level mutable container not registered with process_state")
            )
    return out


def _registers_process_state(tree: ast.AST) -> bool:
    for call in call_nodes(tree):
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "register"
            and isinstance(func.value, ast.Name)
            and func.value.id == "process_state"
        ):
            return True
    return False


def _assign_name(stmt: ast.stmt) -> str | None:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


#: crawlutron.models or crawlutron.vocabulary imports a higher layer.
RULE_MODELS_VOCAB_IMPORT_NOTHING_ABOVE = Rule(
    name="models-and-vocabulary-import-nothing-above",
    find=_upward_imports,
    summary="crawlutron.models or crawlutron.vocabulary imports a higher layer",
    scope=_models_vocab_scope,
    skip=_SKIP_UPWARD,
    bite=lambda: Bites(
        "from crawlutron.service import Engine\n",
        "imported above models/vocabulary",
    ),
    clean=lambda: ["from crawlutron.models import CrawlJob\n"],
)

#: fastapi imported outside packages/rest.
RULE_FASTAPI_REST_ONLY = Rule(
    name="fastapi-importable-only-in-rest",
    find=_fastapi_imports,
    summary="fastapi imported outside packages/rest",
    scope=_scope,
    skip=_SKIP_FASTAPI,
    bite=lambda: Bites("from fastapi import FastAPI\n", "fastapi"),
    clean=lambda: ["import httpx\n"],
)

#: httpx or curl_cffi imported outside the egress module.
RULE_EGRESS_ONLY_FETCHERS = Rule(
    name="egress-only-through-fetchers",
    find=_egress_imports,
    summary="httpx or curl_cffi imported outside the egress module",
    scope=_scope,
    skip=_SKIP_EGRESS,
    bite=lambda: Bites("import httpx\n", "httpx"),
    clean=lambda: ["from pathlib import Path\n"],
)

#: crawlutron.config.settings imported below the service layer.
RULE_SETTINGS_AT_OR_ABOVE_SERVICE = Rule(
    name="settings-only-at-or-above-service-layer",
    find=_settings_imports,
    summary="crawlutron.config.settings imported below the service layer",
    scope=_scope,
    skip=_SKIP_SETTINGS,
    bite=lambda: Bites(
        "from crawlutron.config.settings import CrawlerSettings\n",
        "config.settings",
    ),
    clean=lambda: ["import json\n"],
)

#: uuid.uuid4() outside storage/ids.py.
RULE_UUID4_ONLY_IDS = Rule(
    name="uuid4-only-in-storage-ids",
    find=_uuid4_calls,
    summary="uuid.uuid4() outside storage/ids.py",
    scope=_scope,
    skip=frozenset(),
    bite=lambda: Bites("import uuid\nx = uuid.uuid4()\n", "uuid.uuid4()"),
    clean=lambda: ["import uuid\nx = uuid.uuid1()\n"],
)

#: write-mode open() outside safety.atomic_write.
RULE_WRITE_MODE_OPEN_ATOMIC = Rule(
    name="write-mode-open-only-via-atomic-write",
    find=_open_calls,
    summary="write-mode open() outside safety.atomic_write",
    scope=_scope,
    skip=frozenset(),
    bite=lambda: Bites('open("out.txt", "w")\n', "open("),
    clean=lambda: ['open("in.txt")\n'],
)

#: ThreadPoolExecutor outside runtime/pool.py.
RULE_THREAD_POOL_ONLY_POOL = Rule(
    name="thread-pool-executor-only-in-pool",
    find=_executor_calls,
    summary="ThreadPoolExecutor outside runtime/pool.py",
    scope=_scope,
    skip=_SKIP_EXECUTOR,
    bite=lambda: Bites(
        "from concurrent.futures import ThreadPoolExecutor\nx = ThreadPoolExecutor()\n",
        "ThreadPoolExecutor",
    ),
    clean=lambda: ["from concurrent.futures import ProcessPoolExecutor\n"],
)

#: subprocess imported outside sanctioned sites.
RULE_SUBPROCESS_SANCTIONED = Rule(
    name="subprocess-only-at-sanctioned-sites",
    find=_subprocess_imports,
    summary="subprocess imported outside sanctioned sites",
    scope=_scope,
    skip=frozenset(),
    bite=lambda: Bites("import subprocess\n", "subprocess"),
    clean=lambda: ["import sys\n"],
)

#: module-level mutable container not registered with process_state.
RULE_MUTABLE_CONTAINERS_REGISTER_STATE = Rule(
    name="module-level-mutable-containers-register-process-state",
    find=_mutable_containers,
    summary="module-level mutable container not registered with process_state",
    scope=_scope,
    skip=_SKIP_MUTABLE,
    bite=lambda: Bites("pending: list = []\n", "process_state"),
    clean=lambda: ["count = 0\n"],
)

LAYER_RULES = (
    RULE_MODELS_VOCAB_IMPORT_NOTHING_ABOVE,
    RULE_FASTAPI_REST_ONLY,
    RULE_EGRESS_ONLY_FETCHERS,
    RULE_SETTINGS_AT_OR_ABOVE_SERVICE,
    RULE_UUID4_ONLY_IDS,
    RULE_WRITE_MODE_OPEN_ATOMIC,
    RULE_THREAD_POOL_ONLY_POOL,
    RULE_SUBPROCESS_SANCTIONED,
    RULE_MUTABLE_CONTAINERS_REGISTER_STATE,
)


def test_layer_gate_holds_on_live_tree() -> None:
    failures: list[str] = []
    for rule in LAYER_RULES:
        found = rule.violations(rule.trees())
        if found:
            failures.append("  " + rule.name + ":\n    " + "\n    ".join(found))
    assert not failures, (
        "layer gate violations on the live tree (fix the surface or shrink a skip set in the same commit):\n"
        + "\n".join(failures)
    )


def test_layer_gate_every_rule_bites() -> None:
    for rule in LAYER_RULES:
        rule.check_bites()
