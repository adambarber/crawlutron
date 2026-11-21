from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable

from scripts.module_graph import IMPORT_ROOTS, _runtime_imports, build_graph
from tests._walk_primitives import offender_at
from tests._walk_readers import call_nodes
from tests.chokepoint_paths import APP_ROOT, REPO_ROOT
from tests.chokepoint_walks import trees_under
from tests.rules import Bites, Rule

#: Enforce the vacuity kill-switch: build_graph raises when it walks nothing.
build_graph()

#: Every walked module as (path, tree): the mechanical scope, never hand-typed.
_WALKED: tuple[tuple[pathlib.Path, ast.Module], ...] = tuple(
    (path, tree) for import_dir, _name in IMPORT_ROOTS for path, tree in trees_under(import_dir)
)

#: models/vocabulary subtrees for the upward-import rule.
_MODELS_DIR = APP_ROOT / "models"
_VOCAB_DIR = APP_ROOT / "vocabulary"
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
#: curl_cffi/os flag constants that denote a write (not O_RDONLY).
_WRITE_FLAG_NAMES = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC", "O_EXCL"})

#: The sanctioned egress family: fetchers, pooled_clients and pooled_cffi may construct clients.
_EGRESS_FAMILY = frozenset(
    {
        _FETCHERS,
        "packages/core/src/crawlutron/transport/pooled_clients.py",
        "packages/core/src/crawlutron/transport/pooled_cffi.py",
    }
)

_SKIP_FASTAPI = frozenset({"packages/core/src/crawlutron/health/readiness.py"})
_SKIP_EGRESS = frozenset(
    {
        "packages/rest/src/crawlutron_rest/cli/__main__.py",
        "packages/rest/src/crawlutron_rest/cli/client.py",
        "packages/core/src/crawlutron/transport/pooled_clients.py",
        "packages/core/src/crawlutron/transport/pooled_cffi.py",
        "packages/sdk/src/crawlutron_sdk/client.py",
    }
)
#: pooled_clients.py imports crawlutron.config.settings at runtime (_default_settings)
#: for pool-config defaults -- a genuine below-service-layer import kept as a skip.
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
_SKIP_MUTABLE = frozenset(
    {
        "packages/core/src/crawlutron/extraction/llm_format.py",
        "packages/core/src/crawlutron/transport/robots.py",
    }
)


def _scope_for(trees: tuple[tuple[pathlib.Path, ast.Module], ...]):
    def scope(*, exclude: frozenset[str] = frozenset()):
        _ = exclude
        return trees

    return scope


_scope = _scope_for(_WALKED)
_models_vocab_scope = _scope_for(_MODELS_VOCAB_TREES)


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


def _extract_mode(call: ast.Call, positional: int) -> str | None:
    """Mode carried by ``call``: the ``positional``-th positional arg or ``mode=`` keyword."""
    if len(call.args) > positional:
        arg = call.args[positional]
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
    found: list[str] = []
    for node in _runtime_imports(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("crawlutron."):
            layer = node.module.split(".")[1]
            if layer not in ("models", "vocabulary"):
                found.append(offender_at(path, node, f"{node.module} imported above models/vocabulary"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("crawlutron."):
                    layer = alias.name.split(".")[1]
                    if layer not in ("models", "vocabulary"):
                        found.append(
                            offender_at(path, node, f"{alias.name} imported above models/vocabulary")
                        )
    return found


def _fastapi_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    node = _import_node(tree, "fastapi")
    if node is None or _rel(path).startswith(_REST_PREFIX):
        return []
    return [offender_at(path, node, "fastapi imported outside packages/rest")]


def _receiver_root(value: ast.expr) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return _receiver_root(value.value)
    return None


def _egress_construction(tree: ast.Module, path: pathlib.Path) -> list[str]:
    hits: list[str] = []
    if _rel(path) in _EGRESS_FAMILY:
        return hits
    for call in call_nodes(tree):
        func = call.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in ("AsyncClient", "Client") and _receiver_root(func.value) == "httpx":
            hits.append(offender_at(path, call, "httpx client constructed outside the egress family"))
        elif func.attr == "AsyncSession" and _receiver_root(func.value) in ("curl_cffi", "cffi_requests"):
            hits.append(offender_at(path, call, "curl_cffi client constructed outside the egress family"))
    return hits


def _settings_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    node = _import_node(tree, "crawlutron.config.settings")
    if node is None:
        return found
    if any(_rel(path).startswith(prefix) for prefix in _ALLOWED_SETTINGS):
        return found
    return [offender_at(path, node, "crawlutron.config.settings imported below the service layer")]


def _uuid4_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    if _rel(path) == _IDS:
        return found
    attr_names: set[str] = set()
    bare_names: set[str] = set()
    for node in _runtime_imports(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                if al.name == "uuid":
                    attr_names.add(al.asname or al.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "uuid":
            for al in node.names:
                if al.name == "uuid4":
                    bare_names.add(al.asname or al.name)
    for call in call_nodes(tree):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "uuid4":
            if isinstance(func.value, ast.Name) and func.value.id in attr_names:
                found.append(offender_at(path, call, "uuid.uuid4() outside storage/ids.py"))
        elif isinstance(func, ast.Name) and func.id in bare_names:
            found.append(offender_at(path, call, "uuid4() outside storage/ids.py"))
    return found


def _open_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for call in call_nodes(tree):
        func = call.func
        if isinstance(func, ast.Name) and func.id == "open":
            mode = _extract_mode(call, 1)
            if mode is None or not any(mode.startswith(wm) for wm in _WRITE_MODES):
                continue
            hits.append(offender_at(path, call, "write-mode open() outside safety.atomic_write"))
            continue
        if not (isinstance(func, ast.Attribute) and func.attr == "open"):
            continue
        if _is_pathlib_open_receiver(func.value):
            mode = _extract_mode(call, 0)
            if mode is None or not any(mode.startswith(write_mode) for write_mode in _WRITE_MODES):
                continue
            hits.append(offender_at(path, call, "write-mode Path.open() outside safety.atomic_write"))
            continue
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and len(call.args) >= 2
            and _has_write_flags(call.args[1])
        ):
            hits.append(offender_at(path, call, "os.open() with write flags outside safety.atomic_write"))
    return hits


def _is_pathlib_open_receiver(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "Path"
        and _receiver_root(value.func.value) == "pathlib"
    )


def _has_write_flags(flags: ast.expr) -> bool:
    if isinstance(flags, ast.Attribute) and isinstance(flags.value, ast.Name) and flags.value.id == "os":
        return flags.attr in _WRITE_FLAG_NAMES
    if isinstance(flags, ast.BinOp) and isinstance(flags.op, ast.BitOr):
        return _has_write_flags(flags.left) or _has_write_flags(flags.right)
    return isinstance(flags, ast.Constant) and isinstance(flags.value, int) and flags.value != 0


def _executor_calls(tree: ast.Module, path: pathlib.Path) -> list[str]:
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
        return []
    return [offender_at(path, node, "ThreadPoolExecutor outside runtime/pool.py")]


def _subprocess_imports(tree: ast.Module, path: pathlib.Path) -> list[str]:
    node = _import_node(tree, "subprocess")
    if node is None or _rel(path) == _SANCTIONED_SUBPROCESS:
        return []
    return [offender_at(path, node, "subprocess outside sanctioned sites")]


def _is_empty_literal(value: ast.expr) -> bool:
    if isinstance(value, (ast.List, ast.Set)):
        return len(value.elts) == 0
    if isinstance(value, ast.Dict):
        return len(value.keys) == 0
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("set", "dict", "list")
        and not value.args
        and not value.keywords
    )


def _mutable_containers(tree: ast.Module, path: pathlib.Path) -> list[str]:
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
        if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.Call)) and _is_empty_literal(value):
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
    bite=lambda: Bites("from crawlutron.service import Engine\n", "imported above models/vocabulary"),
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

#: httpx/curl_cffi client construction outside the egress family.
RULE_EGRESS_ONLY_FETCHERS = Rule(
    name="egress-only-through-fetchers",
    find=_egress_construction,
    summary="httpx/curl_cffi client constructed outside the egress family",
    scope=_scope,
    skip=_SKIP_EGRESS,
    bite=lambda: Bites("import httpx\nhttpx.AsyncClient(timeout=5)\n", "httpx client constructed"),
    clean=lambda: ["import httpx\n"],
)

#: crawlutron.config.settings imported below the service layer.
RULE_SETTINGS_AT_OR_ABOVE_SERVICE = Rule(
    name="settings-only-at-or-above-service-layer",
    find=_settings_imports,
    summary="crawlutron.config.settings imported below the service layer",
    scope=_scope,
    skip=_SKIP_SETTINGS,
    bite=lambda: Bites("from crawlutron.config.settings import CrawlerSettings\n", "config.settings"),
    clean=lambda: ["import json\n"],
)

#: uuid.uuid4() or a bare uuid4() outside storage/ids.py.
RULE_UUID4_ONLY_IDS = Rule(
    name="uuid4-only-in-storage-ids",
    find=_uuid4_calls,
    summary="uuid.uuid4() or a bare uuid4() outside storage/ids.py",
    scope=_scope,
    skip=frozenset(),
    bite=lambda: Bites("from uuid import uuid4\nx = uuid4()\n", "uuid4()"),
    clean=lambda: ["import uuid\nx = uuid.uuid1()\n"],
)

#: write-mode open() / Path.open() / os.open() outside safety.atomic_write.
RULE_WRITE_MODE_OPEN_ATOMIC = Rule(
    name="write-mode-open-only-via-atomic-write",
    find=_open_calls,
    summary="write-mode open() / Path.open() / os.open() outside safety.atomic_write",
    scope=_scope,
    skip=frozenset(),
    bite=lambda: Bites('pathlib.Path("out.txt").open("w")\n', "Path.open("),
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
    bite=lambda: Bites("_inflight: set = set()\n", "process_state"),
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
