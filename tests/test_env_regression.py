"""Never re-derive env numbers without the parser: bare ``int()``/``float()`` on an
env read is the config-typo boot crash the parser absorbs, and three shapes are
banned because name-aliasing defeats shape-only matching.
"""

import ast

from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import (
    app_trees,
    bare_callee,
    call_nodes,
    nodes_of,
    offender_at,
    parent_map,
)


def _get_receiver(call: ast.Call) -> ast.expr | None:
    """The receiver of a ``<anything>.get(...)`` call, or None if it is not one: the
    shape both env detectors match on, returned (not a bool) so the caller can narrow
    ``call.func`` instead of re-deriving the attr-get at each site.
    """
    if isinstance(call.func, ast.Attribute) and call.func.attr == "get":
        return call.func.value
    return None


def _is_env_get(node: ast.AST) -> bool:
    """True for ``os.environ.get(...)`` -- the direct env face."""
    if not isinstance(node, ast.Call):
        return False
    receiver = _get_receiver(node)
    return (
        isinstance(receiver, ast.Attribute)
        and receiver.attr == "environ"
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "os"
    )


def _is_env_source(node: ast.AST) -> bool:
    """Any direct env read: ``os.environ.get(...)``, ``os.getenv(...)``,
    ``os.environ[...]``."""
    if _is_env_get(node):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if getattr(func, "id", "") == "getenv" or getattr(func, "attr", "") == "getenv":
            return True
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    )


def _is_bare_environ(node: ast.AST) -> bool:
    """True for a bare ``os.environ`` reference: a mapping handed off or spread."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_source_nodes(tree: ast.AST) -> list[ast.AST]:
    """Each env read once; the ``os.environ`` inside a get/subscript is not a second read."""
    parents = parent_map(tree)
    found: list[ast.AST] = []
    for node in nodes_of(tree):
        if not (_is_env_source(node) or _is_bare_environ(node)):
            continue
        parent = parents.get(id(node))
        if _is_bare_environ(node) and getattr(parent, "value", None) is node:
            if isinstance(parent, ast.Attribute | ast.Subscript):
                continue
        found.append(node)
    return found


def _env_get_aliases(tree: ast.AST) -> set[str]:
    """Names assigned directly from an env read in this module's tree."""
    names: set[str] = set()
    for node in nodes_of(tree):
        if isinstance(node, ast.Assign) and _is_env_source(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_env_source(node.value):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def test_env_alias_escape_bites():
    """Shape 3 must fire on the read-back that shapes 1-2 structurally miss."""
    bad = ast.parse('import os\n\ndef f():\n    raw = os.environ.get("CRAWLUTRON_X")\n    return int(raw)\n')
    assert _env_get_aliases(bad) == {"raw"}
    ok = ast.parse('def f(limits):\n    raw = limits.get("max_bytes")\n    return int(raw)\n')
    assert not _env_get_aliases(ok)
    subscript = ast.parse(
        'import os\n\ndef f():\n    raw = os.environ["CRAWLUTRON_X"]\n    return int(raw)\n'
    )
    assert _env_get_aliases(subscript) == {"raw"}


def _is_crawlutron_get(node: ast.expr) -> bool:
    """True for ``<mapping>.get("CRAWLUTRON_...")`` under any alias."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    first = node.args[0]
    return (
        _get_receiver(node) is not None
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith("CRAWLUTRON_")
    )


def test_no_bare_env_int_or_float_in_app():
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        env_aliases = _env_get_aliases(tree)
        for node in call_nodes(tree):
            callee = bare_callee(node)
            if callee not in ("int", "float"):
                continue
            for arg in node.args:
                if _is_crawlutron_get(arg):
                    offenders.append(offender_at(path, node, f"bare {callee} on CRAWLUTRON_* mapping read"))
                elif _is_env_get(arg):
                    offenders.append(offender_at(path, node, f"bare {callee} on os.environ.get"))
                elif isinstance(arg, ast.Name) and arg.id in env_aliases:
                    offenders.append(offender_at(path, node, f"bare {callee} on env-read alias {arg.id!r}"))
    assert not offenders, "Bare env int/float reintroduced: " + "; ".join(offenders)


# --------------------------------------------------------------------------- #142: the operator
# token is read through its settings home


def test_operator_token_read_through_settings_home():
    """#142: ``CRAWLUTRON_KEY_ADMIN_TOKEN`` is declared once and every read routes through that home. A raw
    ``os.environ.get`` re-declares the token's source and drifts the moment the settings home is re-homed
    -- the caller-side copy silently keeps reading the environment.
    """
    offenders = []
    for path, tree in app_trees(exclude=frozenset()):
        rel = str(path)
        if rel.endswith("packages/core/src/crawlutron/config/settings.py"):
            continue
        for node in call_nodes(tree):
            first = node.args[0] if node.args else None
            is_token_read = (
                isinstance(first, ast.Constant)
                and first.value == "CRAWLUTRON_KEY_ADMIN_TOKEN"
                and _is_env_get(node)
            )
            if is_token_read:
                offenders.append(
                    offender_at(path, node, "CRAWLUTRON_KEY_ADMIN_TOKEN read outside CrawlerSettings")
                )
    assert not offenders, "Operator token read outside its settings home: " + ", ".join(offenders)


# --------------------------------------------------------------------------- #1407: admit-or-refuse
# The settings home owns env reads; every other reader is a named, down-only narrowing.
_ENV_HOME = frozenset(
    {
        "packages/core/src/crawlutron/config/env.py",
        "packages/core/src/crawlutron/config/settings.py",
    }
)
_ENV_READ_ALLOWANCES: dict[str, int] = {
    # Test-only reset interlock: read at call time, deliberately outside the cache.
    "packages/core/src/crawlutron/config/settings_cache.py": 1,
    # CLI boundary: caller-side auth/endpoint env, not server settings fields.
    "packages/rest/src/crawlutron_rest/cli/__main__.py": 2,
    # safe_url is standalone (no core dependency); its SSRF escape hatch stays local.
    "packages/safe-url/src/safe_url/host_policy.py": 1,
}


def _env_read_offenders(trees) -> list[str]:
    """Ungoverned env reads: outside the home and allowances, or a drifted allowance."""
    offenders: list[str] = []
    for path, tree in trees:
        relative = relative_posix(path)
        if relative in _ENV_HOME:
            continue
        reads = _env_source_nodes(tree)
        allowance = _ENV_READ_ALLOWANCES.get(relative)
        if allowance is None:
            offenders.extend(
                offender_at(path, node, "direct env read outside the settings home") for node in reads
            )
        elif len(reads) != allowance:
            offenders.append(f"{relative}: env-read allowance is {len(reads)}, ratchet says {allowance}")
    for relative, allowance in _ENV_READ_ALLOWANCES.items():
        if allowance < 1:
            offenders.append(f"{relative}: sealed allowance row must be deleted, not left at {allowance}")
    return offenders


# --------------------------------------------------------------------------- #1407: the whole operation class


def test_every_env_read_shape_is_one_detected_class():
    """The four env faces (get, getenv, subscript, bare mapping) are one read class."""
    tree = ast.parse(
        "import os\n\n\ndef f():\n"
        '    a = os.environ.get("CRAWLUTRON_A")\n'
        '    b = os.getenv("CRAWLUTRON_B")\n'
        '    c = os.environ["CRAWLUTRON_C"]\n'
        "    d = os.environ\n"
        "    return a, b, c, d\n"
    )
    assert len(_env_source_nodes(tree)) == 4


def test_direct_env_read_outside_settings_home_is_refused():
    """A raw env read in a production module with no allowance is an offender."""
    path = REPO_ROOT / "packages/core/src/crawlutron/transport/rogue.py"
    tree = ast.parse('import os\n\nx = os.getenv("CRAWLUTRON_X")\n')
    offenders = _env_read_offenders(((path, tree),))
    assert offenders and offenders[0].endswith("direct env read outside the settings home")


def test_env_read_allowance_is_exact_both_ways():
    """An allowance is exact: adding OR deleting a read must move its row."""
    path = REPO_ROOT / "packages/rest/src/crawlutron_rest/cli/__main__.py"
    tree = ast.parse('import os\n\nx = os.getenv("CRAWLUTRON_API_KEY")\n')
    offenders = _env_read_offenders(((path, tree),))
    assert any("allowance is 1, ratchet says 2" in o for o in offenders)


def test_no_ungoverned_env_reads_in_app():
    """The operation class is closed over app_trees: only the home and admitted rows read env."""
    offenders = _env_read_offenders(app_trees(exclude=frozenset()))
    assert not offenders, "Ungoverned env reads: " + "; ".join(offenders)
