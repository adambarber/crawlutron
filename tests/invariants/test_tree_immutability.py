"""The test suite never mutates the production tree it inspects."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import call_nodes, nodes_of, offender_at, resolved_expr_name
from tests.rules import Bites, Rule, Trips
from tests.trees import tree_under_tests as tests_tree


#: One owner for the answer, so the rules below
def _tests_tree(*, exclude: frozenset[str]) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The test tree, swept whole; this file's own import of the shared one."""
    return tests_tree(exclude=exclude)


# =========================================================================== NO TEST MAY MUTATE THE
_SERVICE_ROOT_NAMES = frozenset({"REPO_ROOT", "SERVICE_ROOT", "HERE", "ROOT"})


# Already inside app/ by construction: the walk helpers hand these out.
_APP_ROOTED_NAMES = frozenset({"APP_ROOT", "TRANSPORT_DIR"})


# Qualifiers whose mutating call takes the path as an ARGUMENT (`os.remove(p)`, `shutil.rmtree(p)`) instead of
# as its receiver.
_FS_MODULE_QUALIFIERS = frozenset({"os", "shutil", "pathlib", "io", "posixpath", "ntpath"})


# Calls that change the filesystem. `str.replace` and `list.remove` are in here too, which costs nothing: a
# call is only reported when its target resolves into app/, and a string is not a path into app/.
_MUTATING_METHODS = frozenset(
    {
        "unlink",
        "write_text",
        "write_bytes",
        "mkdir",
        "rmdir",
        "rename",
        "replace",
        "touch",
        "symlink_to",
        "hardlink_to",
        "link_to",
        "chmod",
        "chown",
        "truncate",
        "rmtree",
        "copy",
        "copy2",
        "copyfile",
        "move",
        "remove",
        "removedirs",
        "makedirs",
    }
)


_MUTATING_FUNCTIONS = frozenset(
    {
        "remove",
        "unlink",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "truncate",
        "rmtree",
        "copy",
        "copy2",
        "copyfile",
        "move",
        "write_text",
        "write_bytes",
        "symlink",
        "link",
        "chmod",
        "mkstemp",
        "mkdtemp",
    }
)


_WRITE_MODE_CHARS = ("w", "a", "x", "+")


_BINDING_PASSES = 4


def _names_app_tree(component: ast.expr) -> bool:
    """Whether one path component selects the `app/` tree.

    A relative literal names the tree when its first segment is ``app``; an
    absolute literal is a real path and any ``app`` segment counts.
    """
    if not (isinstance(component, ast.Constant) and isinstance(component.value, str)):
        return False
    parts = [part for part in component.value.split("/") if part not in ("", ".")]
    if not parts:
        return False
    return parts[0] == "app" or (component.value.startswith("/") and "app" in parts)


def _path_components(expr: ast.expr) -> list[ast.expr] | None:
    """Flatten ``a / b / c``, ``os.path.join(a, b)``, and ``Path(a)`` into components.

    The elements are expressions, which is what every consumer classifies them
    as; only the wider node type was stopping them from saying so.
    """
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        head = _path_components(expr.left)
        return None if head is None else [*head, expr.right]
    if isinstance(expr, ast.Call):
        name = getattr(expr.func, "attr", None) or getattr(expr.func, "id", None)
        if name in {"Path", "PurePath"} and expr.args:
            # `Path(APP_ROOT / "x")` is the same path as `APP_ROOT / "x"`, so the wrapped expression is
            # flattened rather than boxed.
            return _path_components(expr.args[0])
        if name == "join" and expr.args:
            head = _path_components(expr.args[0])
            return None if head is None else [*head, *expr.args[1:]]
    return [expr]


def _tree_role(
    components: list[ast.expr],
    anchors: set[str],
    rooted: set[str],
) -> str:
    """Classify a path expression as the app tree, a service root, or neither.

    The unnamed-anchor case is the point: a path whose first filesystem anchor
    this cannot name (`tmp_path`, a parameter) must not be assumed to be `app/`.
    """
    for position, component in enumerate(components):
        if isinstance(component, ast.Name) and component.id in rooted:
            return "app"
        if isinstance(component, ast.Attribute) and component.attr in rooted:
            return "app"
        if isinstance(component, (ast.Name, ast.Attribute)):
            name = component.id if isinstance(component, ast.Name) else component.attr
            if name in anchors:
                following = components[position + 1 : position + 2]
                return "app" if following and _names_app_tree(following[0]) else "root"
            return "other"
        if _names_app_tree(component):
            return "app"
        return "other"
    return "other"


def _absorb_path_aliases(tree: ast.AST, anchors: set[str], rooted: set[str]) -> bool:
    """Add names this module binds to a service root or to `app/`."""
    grew = False
    for node in nodes_of(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        components = _path_components(node.value)
        if components is None:
            continue
        role = _tree_role(components, anchors, rooted)
        if role not in {"app", "root"}:
            continue
        names = (
            [target.id for target in node.targets if isinstance(target, ast.Name)]
            if isinstance(node, ast.Assign)
            else [node.target.id]
            if isinstance(node.target, ast.Name)
            else []
        )
        target_set = rooted if role == "app" else anchors
        for name in names:
            if name not in target_set:
                target_set.add(name)
                grew = True
    return grew


def _app_path_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """The service-root and app-tree names this module can name."""
    anchors = set(_SERVICE_ROOT_NAMES)
    rooted = set(_APP_ROOTED_NAMES)
    for _ in range(_BINDING_PASSES):
        if not _absorb_path_aliases(tree, anchors, rooted):
            break
    return anchors, rooted


def _opens_for_writing(node: ast.Call) -> bool:
    """Whether `open(...)` is called with a mode that writes."""
    if len(node.args) < 2:
        return False
    mode = node.args[1]
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(char in mode.value for char in _WRITE_MODE_CHARS)
    )


def _app_tree_mutation_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Report every call in this module that can mutate a path under `app/`."""
    anchors, rooted = _app_path_names(tree)
    offenders: list[str] = []
    for node in call_nodes(tree):
        callee = node.func
        if isinstance(callee, ast.Attribute):
            name = callee.attr
            if name not in _MUTATING_METHODS and not (name == "open" and _opens_for_writing(node)):
                continue
            qualifier = resolved_expr_name(tree, callee.value).rsplit(".", 1)[-1]
            targets = node.args if qualifier in _FS_MODULE_QUALIFIERS else [callee.value]
        elif isinstance(callee, ast.Name) and (
            callee.id in _MUTATING_FUNCTIONS or (callee.id == "open" and _opens_for_writing(node))
        ):
            targets = node.args
        else:
            continue
        if not targets or _tree_role(_path_components(targets[0]) or [], anchors, rooted) != "app":
            continue
        offenders.append(offender_at(path, node, f"mutates the production tree: {ast.unparse(callee)}()"))
    return offenders


PLANTED_MUTATION = (
    "from scripts import jscpd_gate as gate\n"
    "\n"
    "def test_migrations_clones_are_not_reported(capsys):\n"
    "    in_migrations = gate.SERVICE_ROOT / 'app' / 'crawl' / 'migrations' / 'zz_probe.py'\n"
    "    in_production = gate.SERVICE_ROOT / 'app' / 'zz_probe.py'\n"
    "    in_migrations.write_text('x')\n"
    "    try:\n"
    "        assert True\n"
    "    finally:\n"
    "        in_production.unlink()\n"
)


MIRRORED_SCRATCH_TREE = (
    "def test_mirrored_tree(tmp_path):\n"
    "    app = tmp_path / 'app'\n"
    "    (app / 'crawl' / 'migrations').mkdir(parents=True)\n"
    "    (app / 'zz_probe.py').write_text('payload')\n"
    "    (tmp_path / 'tests' / 'fixtures' / 'app' / 'x.py').write_text('fixture')\n"
    "    Path = __import__('pathlib').Path\n"
    "    (Path(__file__).resolve().parent / '_jscpd_gate_helpers.py').read_text()\n"
)


TESTS_MUST_NOT_MUTATE_APP = Rule(
    name="tests-do-not-mutate-app",
    find=_app_tree_mutation_offenders,
    scope=_tests_tree,
    summary=(
        "mirror the path shape under tmp_path and point the code under test at the mirror: "
        "a test that mutates app/ turns every concurrent tree walk into a list-then-read race, "
        "and the detectors that lose it report nothing while looking green"
    ),
    bite=lambda: (
        # The real offender -- a fixed path under the live tree, written and unlinked from a test -- so the
        # sweep has to catch the exact spelling in the tree, not a paraphrase of it.
        Bites(PLANTED_MUTATION, marker="mutates the production tree", count=2),
        # The qualified spellings, which take the path as an argument instead of as a receiver, and the
        # aliased ones where the root is bound before it is used.
        Trips(
            "from pathlib import Path\n"
            "from chokepoint_walks import APP_ROOT\n"
            "import shutil\n"
            "\n"
            "def wipe():\n"
            "    shutil.rmtree(APP_ROOT)\n"
            "    Path.cwd()\n"
            "    Path(APP_ROOT / 'settings.py').replace(APP_ROOT / 'settings.py.bak')\n"
            "    open(APP_ROOT / 'settings.py', 'w').write('x')\n",
            count=3,
        ),
        Trips(
            "from pathlib import Path\n"
            "import os\n"
            "\n"
            "ROOT = Path(__file__).resolve().parent.parent\n"
            "APP = ROOT / 'app'\n"
            "\n"
            "def rename_it():\n"
            "    (APP / 'crawl' / 'ids.py').rename(APP / 'crawl' / 'ids_v2.py')\n"
            "    os.remove(ROOT / 'app' / 'main.py')\n",
            count=2,
        ),
    ),
    clean=lambda: (
        # A test's own scratch tree is not the production tree, and `read_text` on an app/ module is a
        # legitimate thing for a detector test to do.
        MIRRORED_SCRATCH_TREE,
    ),
)


def test_tests_do_not_mutate_the_production_tree():
    """No test writes, unlinks, renames, or replaces a path inside `app/`.

    Tree-wide, and read from the tree rather than from a file list, so the
    sweep covers a test added tomorrow with nothing to register.
    """
    TESTS_MUST_NOT_MUTATE_APP.scan()


def test_tree_detectors_bite():
    """Bites: the shapes that actually shipped, and the near-misses beside them.

    The first is the real offender (making `app/` move mid-suite); the rest are
    ways a fix could regress the rule into a no-op or false alarm.
    """
    TESTS_MUST_NOT_MUTATE_APP.check_bites()
