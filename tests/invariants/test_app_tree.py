"""The traversal every other check depends on.

If one module parses 105 app/ files per call, a suite of ~100 detectors pays
for that parse hundreds of times. These checks pin the walk to a single
traversal and forbid the test suite from mutating the tree it inspects."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    TESTS_DIR,
    app_python_files,
    app_trees,
    call_nodes,
    from_imports,
    nodes_of,
    offender_at,
    plain_imports,
    resolved_expr_name,
    trees_under,
)
from tests.invariants._shared import assert_trees_clean

# ===========================================================================
# NO TEST MAY MUTATE THE PRODUCTION TREE
#
# `tests/test_jscpd_gate.py` proved the jscpd `ignore` glob by planting two
# clone pairs at FIXED paths inside the live `app/` tree and unlinking them in a
# `finally`. That made `app/` a directory that MOVES while the suite runs, and
# anything that walks it concurrently becomes a list-then-read race: it globs
# the file list, the probe disappears, and `read_text` raises. Measured with 6
# concurrent copies of that file against 6 concurrent copies of this one, 10
# tests failed with `FileNotFoundError: .../app/crawl/migrations/zz_probe_clone.py`
# -- and the victims were the tree-wide chokepoint detectors (await-in-lock,
# bare `re.compile`, `regex` import), so they reported nothing for the entire run
# while appearing green. A detector that cannot see the tree cannot hold it.
#
# So the class closes mechanically rather than one file at a time: this sweep
# reads every module under tests/ and reports any call that can mutate a path
# under app/. It is a name-and-shape check over statically resolvable
# expressions; a path handed in as a parameter and written by a caller outside
# tests/ is invisible to it, which is why a name it DOES resolve is a hard
# failure and not a warning.
#
# The sanctioned way to get a real production path SHAPE is to mirror the
# layout under `tmp_path` and point the code under test at the mirror. A
# `tmp_path / "app"` fixture is deliberately NOT this rule: a scratch directory
# a test builds itself is not the production tree.
# ===========================================================================

# A path built from one of these is anchored at the service root, so the
# segment joined onto it next names a real tree. Aliases a module binds to one
# of them are absorbed below, so `ROOT = HERE.parent` is the same anchor.
_SERVICE_ROOT_NAMES = frozenset({"REPO_ROOT", "SERVICE_ROOT", "HERE", "ROOT"})


# Already inside app/ by construction: the walk helpers hand these out.
_APP_ROOTED_NAMES = frozenset({"APP_ROOT", "STRATEGIES_DIR"})


# Qualifiers whose mutating call takes the path as an ARGUMENT
# (`os.remove(p)`, `shutil.rmtree(p)`) instead of as its receiver.
_FS_MODULE_QUALIFIERS = frozenset({"os", "shutil", "pathlib", "io", "posixpath", "ntpath"})


# Calls that change the filesystem. `str.replace` and `list.remove` are in here
# too, which costs nothing: a call is only reported when its target resolves
# into app/, and a string is not a path into app/.
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


# An `open()` is a write only with a mode that says so; a mode this cannot read
# is treated as a read, because `Path.read_text`/`open(p).read()` on an app/
# module is a legitimate thing for a detector test to do.
_WRITE_MODE_CHARS = ("w", "a", "x", "+")


# Alias chains (`ROOT = HERE.parent`, `APP = REPO_ROOT / "app"`) are resolved
# to a fixed point rather than recursively; four passes is far past the longest
# chain a module can express in one scope and bounds the walk either way.
_BINDING_PASSES = 4


def _names_app_tree(component: ast.expr) -> bool:
    """Whether one path component selects the `app/` tree.

    A relative literal names the tree when its FIRST segment is `app`, so
    `tests/fixtures/app/x.py` is a fixture and not a production path. An
    absolute literal is a real filesystem path and any `app` segment in it
    counts.
    """
    if not (isinstance(component, ast.Constant) and isinstance(component.value, str)):
        return False
    parts = [part for part in component.value.split("/") if part not in ("", ".")]
    if not parts:
        return False
    return parts[0] == "app" or (component.value.startswith("/") and "app" in parts)


def _path_components(expr: ast.expr) -> list[ast.expr] | None:
    """Flatten `a / b / c`, `os.path.join(a, b)`, and `Path(a)` into components.

    The elements are expressions -- a name, an attribute, a call result -- which
    is what every consumer classifies them as, so the wider node type was the
    only thing stopping them from saying so.
    """
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        head = _path_components(expr.left)
        return None if head is None else [*head, expr.right]
    if isinstance(expr, ast.Call):
        name = getattr(expr.func, "attr", None) or getattr(expr.func, "id", None)
        if name in {"Path", "PurePath"} and expr.args:
            # `Path(APP_ROOT / "x")` is the same path as `APP_ROOT / "x"`, so
            # the wrapped expression is flattened rather than boxed.
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

    Returns ``"app"`` for anything that lands in `app/`, ``"root"`` for a bare
    service root (so an alias of one can be absorbed), and ``"other"`` for every
    expression whose first filesystem anchor this cannot name -- `tmp_path`, a
    parameter, a value returned by a call. That last case is the whole point:
    an unnamed anchor cannot be assumed to be the production tree.
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


def test_tests_do_not_mutate_the_production_tree():
    """No test writes, unlinks, renames, or replaces a path inside `app/`.

    Tree-wide, and read from the tree rather than from a file list, so the
    sweep covers a test added tomorrow with nothing to register.
    """
    assert_trees_clean(
        _app_tree_mutation_offenders,
        "mirror the path shape under tmp_path and point the code under test at the mirror: "
        "a test that mutates app/ turns every concurrent tree walk into a list-then-read race, "
        "and the detectors that lose it report nothing while looking green",
        trees=trees_under(TESTS_DIR),
    )


def test_app_tree_mutation_detector_bites():
    """Bites: the shapes that actually shipped, and the near-misses beside them.

    The first three are the real offender -- a fixed path under the live tree,
    written and unlinked from a test -- so the sweep has to catch the exact
    spelling in the tree, not a paraphrase of it. The rest are the ways a
    plausible fix could regress the rule into either a no-op or a false alarm.
    """
    planted = ast.parse(
        "import _jscpd_gate as gate\n"
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
    offenders = _app_tree_mutation_offenders(planted, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(offenders) == 2, offenders
    assert all("write_text" in found or "unlink" in found for found in offenders), offenders

    module_qualified = ast.parse(
        "from pathlib import Path\n"
        "from chokepoint_walks import APP_ROOT\n"
        "import shutil\n"
        "\n"
        "def wipe():\n"
        "    shutil.rmtree(APP_ROOT)\n"
        "    Path.cwd()\n"
        "    Path(APP_ROOT / 'settings.py').replace(APP_ROOT / 'settings.py.bak')\n"
        "    open(APP_ROOT / 'settings.py', 'w').write('x')\n"
    )
    qualified = _app_tree_mutation_offenders(module_qualified, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(qualified) == 3, qualified

    aliased = ast.parse(
        "from pathlib import Path\n"
        "import os\n"
        "\n"
        "ROOT = Path(__file__).resolve().parent.parent\n"
        "APP = ROOT / 'app'\n"
        "\n"
        "def rename_it():\n"
        "    (APP / 'crawl' / 'ids.py').rename(APP / 'crawl' / 'ids_v2.py')\n"
        "    os.remove(ROOT / 'app' / 'main.py')\n"
    )
    assert len(_app_tree_mutation_offenders(aliased, REPO_ROOT / "tests" / "_synthetic.py")) == 2

    scratch = ast.parse(
        "def test_mirrored_tree(tmp_path):\n"
        "    app = tmp_path / 'app'\n"
        "    (app / 'crawl' / 'migrations').mkdir(parents=True)\n"
        "    (app / 'zz_probe.py').write_text('payload')\n"
        "    (tmp_path / 'tests' / 'fixtures' / 'app' / 'x.py').write_text('fixture')\n"
        "    Path = __import__('pathlib').Path\n"
        "    (Path(__file__).resolve().parent / 'test_jscpd_gate.py').read_text()\n"
    )
    assert not _app_tree_mutation_offenders(scratch, REPO_ROOT / "tests" / "_synthetic.py"), (
        "a test's own scratch tree is not the production tree"
    )


# ===========================================================================
# THE CHOKEPOINT OWNS ITS TRAVERSAL (one snapshot, shared by every detector)
#
# `app_trees` was a generator that re-globbed, re-read and re-parsed all 105 app/
# modules on every call, and each of the ~7 tree-walking detectors called it, so
# one session paid for the same parse about seven times over. Measured on
# tests/invariants/ alone: 37.7s of a 49.4s file, led by
# test_property_registry_fingerprints_are_observed 8.06s,
# test_documented_browser_knobs_have_consumers 7.49s,
# test_robots_policy_has_one_implementation 7.45s, test_bool_env_has_one_implementation
# 5.28s, test_public_target_has_one_implementation 3.95s,
# test_property_registry_rejects_deleted_fingerprint 3.63s and
# test_url_returning_helpers_require_host_policy 1.83s.
#
# The cost was the smaller half of the defect. Because every walk observed the
# tree at a DIFFERENT INSTANT, "list the files" and "read the files" were two
# moments, and anything that appeared or vanished between them raised
# FileNotFoundError inside a detector instead of being reported -- the
# list-then-read race that tests/test_jscpd_gate.py used to cause whenever it
# planted a file in app/ beside a chokepoint sweep. Same root cause, one fix:
# the traversal is executed once per exclude set and every detector is handed
# the same ast.Module objects.
#
# The three tests below demonstrate that by execution. None of them writes to
# app/ -- the sweep above forbids it -- so the race is reproduced by making the
# filesystem refuse to answer, which is what a vanished file looks like from
# inside a list-then-read walk.
#
# ONE NODE SEQUENCE PER ROOT, which is the same fix one level deeper. Owning
# the snapshot removed the re-GLOB and the re-READ; the re-WALK underneath it
# was still there, and it was the larger half: 40 detectors and 91 `ast.walk`
# sites over ~105 modules meant forty passes over every node where one would
# do. `chokepoint_walks.nodes_of` materializes `tuple(ast.walk(root))` once per
# root and every detector iterates that, so a detector's own logic is untouched
# and what is removed is the walk it would have repeated. Sub-walks -- a
# detector narrowing to one function and walking that -- are memoized on the
# same terms, and they are where the sharing pays most, because the same
# function body is walked by many detectors.
#
# The last two tests below are the receipt for that. A memo is the easiest
# possible way to make forty detectors fast and blind at once: return an empty
# tuple for an unwalked root and every one of them reports nothing, every one
# of them stays green, and the suite is faster. So they check identity (one
# materialization, shared), element-wise equality against a FRESH `ast.walk`
# (the same objects in the same order, not a subset or a re-ordering), and
# coverage of every module in the snapshot (no root is silently absent).
# ===========================================================================


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
    # A distinct name from the loop above: the two are separate traversals of
    # the same tree, and one binding for both meant the second yield carried the
    # first loop's node type.
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


def test_tests_import_their_helpers_by_package_path():
    """No test module reaches a sibling helper by bare name.

    Tree-wide over ``tests/``, so a helper added tomorrow is covered by existing
    the same way, and read from the tree rather than a file list.
    """
    assert_trees_clean(
        _bare_sibling_import_offenders,
        "import the helper through its package path (tests.<pkg>.<module>): a bare sibling name "
        "resolves only because pytest put this file's directory on sys.path, which is invisible to "
        "both E402 and the type checker",
        trees=trees_under(TESTS_DIR),
    )


def test_bare_sibling_import_detector_bites():
    """Bites: the shipped spellings, and the neighbors that must stay quiet.

    The first is the real thing -- the exact ``from _chain_policies import
    chain_policy`` that two test modules carried. The rest pin both directions:
    the package form, a stdlib import, and a third-party import all name a
    module that is not a test file and must be left alone, so the rule cannot
    degenerate into "no unqualified imports".
    """
    path = REPO_ROOT / "tests" / "_synthetic.py"
    planted = ast.parse(
        "from _chain_policies import chain_policy\n"
        "import _jscpd_gate as gate\n"
        "from _policy_factories import _policy\n"
    )
    found = _bare_sibling_import_offenders(planted, path)
    assert len(found) == 3, f"one offender per import statement, got {found}"

    quiet = ast.parse(
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "import httpx\n"
        "import pytest\n"
        "from tests._policy_factories import _policy\n"
        "from tests.crawl._chain_policies import chain_policy\n"
        "from app.crawl.policy import read_policy\n"
    )
    assert not _bare_sibling_import_offenders(quiet, path), (
        "package, stdlib and third-party forms are not offenders"
    )


def test_every_detector_sees_the_same_app_trees():
    """Two walks are one snapshot: the SAME ast.Module objects, not two parses.

    Identity, not equality. A second parse of an unchanged file compares equal
    for every field a detector looks at, so a detector cannot tell a
    re-observation from a changed tree -- which is exactly the distinction the
    shared snapshot exists to draw.
    """
    first = app_trees()
    second = app_trees()
    assert first, "the app tree walk found no modules to compare"
    assert second is first, "each caller is being handed its own walk of app/"
    assert all(one is other for one, other in zip(first, second, strict=True)), (
        "the same module parsed twice is not a shared snapshot"
    )


def test_the_app_tree_is_read_once_however_many_detectors_walk_it(monkeypatch):
    """N detectors cost one read per module, not N.

    Bounded by the module count rather than equal to it, so the assertion is
    true whether this test took the snapshot or an earlier one in the session
    did. Today each walk re-reads the tree, so three walks cost three times the
    modules and this is where the 37.7s went.
    """
    reads = _app_reads(monkeypatch)
    for _ in range(3):
        # Consumed, not merely called: a walk nobody iterates reads nothing, and
        # this test has to be able to see a re-walk.
        tuple(app_trees())
        tuple(app_python_files())
    assert len(reads) <= len(app_python_files()), (
        f"the app tree was read {len(reads)} times for {len(app_python_files())} modules; "
        "the traversal has to be owned by the chokepoint, not repeated per detector"
    )


def test_a_later_walk_cannot_observe_a_tree_that_moved(monkeypatch):
    """The race half: once the snapshot exists, no walk touches the tree again.

    The filesystem is made to refuse rather than made to change, so this proves
    the property without a second offender. A walk that re-globbed raises here;
    a walk that returns its snapshot cannot, because a file that disappears
    after the snapshot is not something a snapshot can observe.
    """
    warm = app_trees()
    assert warm, "the app tree walk found no modules"

    def refuse(*_args, **_kwargs):
        raise AssertionError("a later walk touched the tree instead of returning its snapshot")

    monkeypatch.setattr(pathlib.Path, "rglob", refuse)
    monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    assert app_trees() is warm


def test_the_path_list_and_the_parsed_trees_are_one_traversal(monkeypatch):
    """Asking for paths does not parse, and asking again costs nothing.

    The line-budget sweep wants the file list and the detectors want the trees;
    both are the same rglob, so the list half must not drag a parse along with
    it and neither may be recomputed per caller.
    """
    reads = _app_reads(monkeypatch)
    paths = app_python_files()
    assert len(reads) == 0, f"listing the tree parsed {len(reads)} modules it did not need parsed"
    trees = app_trees()
    assert [path for path, _tree in trees] == list(paths), "the trees are not the files that were listed"
    # Exactly one read per module when this test took the snapshot, and none at
    # all when an earlier test in the session already did: both are "once", and
    # the assertion has to hold whichever order the session ran in.
    assert len(reads) in (0, len(trees)), (
        f"{len(reads)} reads for {len(trees)} modules; the snapshot is read once, not once per caller"
    )
    assert app_python_files() is paths, "the file list is re-globbed per caller"
    assert app_trees() is trees, "the tree snapshot is rebuilt per caller"


def test_the_tests_tree_is_walked_once_too():
    """The tests/ sweep is the same deal: five detectors, one walk.

    Same return type as ``app_trees`` for the same reason, and the five
    callers of ``trees_under(TESTS_DIR)`` now share one snapshot instead of
    each re-parsing the tree they are all reading.
    """
    first = trees_under(TESTS_DIR)
    assert first, "the tests/ walk found no modules"
    assert trees_under(TESTS_DIR) is first, "the tests/ tree is re-walked per detector"
    assert trees_under(TESTS_DIR / "crawl") is not first, "a different tree must be its own snapshot"


def test_every_detector_sees_the_same_nodes():
    """One materialization per root, and it is the one ``ast.walk`` yields.

    Three claims, each of which fails differently. IDENTITY, so a second walk
    costs nothing and the sequence cannot have been rebuilt underneath a
    detector mid-iteration. ELEMENT-WISE EQUALITY against a fresh
    ``ast.walk(tree)``, so what is shared is the same objects in the same
    breadth-first order rather than a subset, a re-ordering or a set. And
    COVERAGE, because a memo that answered an empty tuple for a root it had not
    walked would be fast, green and blind -- the failure this whole file exists
    to make impossible, reintroduced through the cache.
    """
    expected_modules = len(app_python_files())
    walked = 0
    for path, tree in app_trees():
        shared = nodes_of(tree)
        assert shared is nodes_of(tree), f"{path.name}: the node sequence is rebuilt per caller"
        assert list(shared) == list(ast.walk(tree)), (
            f"{path.name}: the shared sequence is not what ast.walk yields"
        )
        assert shared, f"{path.name}: a materialized walk came back empty"
        walked += 1
    assert walked == expected_modules, (
        f"the shared node sequence covers {walked} of {expected_modules} app modules"
    )


def test_a_narrowed_walk_is_shared_too():
    """The sub-walks share on the same terms, and they are the common case.

    Most detectors here narrow to one function and walk that rather than
    walking the module, and those roots are memoized exactly like a module is.
    Two definitions per module rather than all of them: the point is that a
    narrowed root behaves like any other, and re-walking every function in the
    tree inside the test that exists to avoid re-walking them would be the
    thing it is checking.
    """
    checked = 0
    for path, tree in app_trees():
        narrowed = [
            node for node in nodes_of(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ][:2]
        for node in narrowed:
            shared = nodes_of(node)
            assert shared is nodes_of(node), f"{path.name}: a narrowed walk is rebuilt per caller"
            assert list(shared) == list(ast.walk(node)), (
                f"{path.name}:{node.lineno}: the narrowed sequence is not what ast.walk yields"
            )
            checked += 1
    assert checked >= 100, (
        f"only {checked} narrowed walks were checked across the tree; the assertion "
        "has to span the snapshot to be evidence about the snapshot"
    )
