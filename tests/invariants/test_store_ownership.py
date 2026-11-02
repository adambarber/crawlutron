"""Browser and store state release through their chokepoints."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    assignment_targets,
    class_defs,
    functions,
    nodes_of,
    offender_at,
    resolved_call_name,
    writes,
)
from tests.invariants._proof_paths import ELSEWHERE as _ELSEWHERE
from tests.rules import Bites, Registry, Rule, Silent

# --- browser instance release ------------------------------------------------- Both the per-attempt reset
# and the service-shutdown close have to forget the same cached fields.
_BROWSER_RELEASE_FIELDS = ("_browser_cm", "_browser", "_browser_reused")


#: The file that owns the fields and the chokepoint today, and therefore the path the bite proof is planted
#: at.
_BROWSER_RELEASE_OWNER = REPO_ROOT / "crawlutron" / "transport" / "browser_driver.py"


#: Both files, not one. Sweeping the pair keeps the invariant whole across the split: a field reintroduced on
#: either side is still caught by whichever file spells the clear.
_BROWSER_RELEASE_OWNERS = frozenset(
    {_BROWSER_RELEASE_OWNER, REPO_ROOT / "crawlutron" / "transport" / "browser.py"}
)


_BROWSER_RELEASE_CHOKEPOINT = "_release_browser"


#: __init__ puts the fields at their initial None; that is not a release.
_BROWSER_RELEASE_EXEMPT = frozenset({"__init__"})


def _browser_release_offenders(tree, path, *, owners, fields, chokepoint):
    # Path == Path, not path.name: comparing a str against a Path is never equal, which would make the whole
    # sweep silently yield nothing.
    if path not in owners:
        return
    for node in functions(tree):
        if node.name in (chokepoint, *_BROWSER_RELEASE_EXEMPT):
            continue
        for inner in ast.walk(node):
            # Only clearing to None is a release. _acquire_browser assigns a real context manager and a real
            # browser, which is the opposite of forgetting them.
            cleared = (
                [
                    t
                    for t in inner.targets
                    if isinstance(t, ast.Attribute)
                    and t.attr in fields
                    and isinstance(inner.value, ast.Constant)
                    and inner.value.value is None
                ]
                if isinstance(inner, ast.Assign)
                else []
            )
            for target in cleared:
                yield offender_at(
                    path,
                    inner,
                    f"{target.attr} is cleared to None in {node.name}() rather than {chokepoint}(); route "
                    f"the release through the chokepoint so every exit path clears the same set of fields",
                )


def _browser_release_source() -> str:
    """The proof's source, with its resets generated from the swept field tuple.

    Written twice it was the same three drift-prone lines, invisible when a
    field added to the tuple left the hand-written proof never mentioning it.
    """
    reset = "\n".join(f"        self.{field} = None" for field in _BROWSER_RELEASE_FIELDS)
    return (
        "class Renderer:\n"
        f"    def __init__(self):\n{reset}\n\n"
        f"    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):\n{reset}\n\n"
        "    async def close(self):\n"
        "        self._browser = None\n"
    )


BROWSER_RELEASE_ONLY_THROUGH_THE_CHOKEPOINT = Rule(
    name="browser-release-only-through-the-chokepoint",
    find=lambda tree, path: _browser_release_offenders(
        tree,
        path,
        owners=_BROWSER_RELEASE_OWNERS,
        fields=_BROWSER_RELEASE_FIELDS,
        chokepoint=_BROWSER_RELEASE_CHOKEPOINT,
    ),
    summary=(
        "_acquire_browser and close have to forget the same cached fields, and "
        "spelling that out twice is how one of them grows a stale field the other "
        "never clears; release every field through _release_browser"
    ),
    bite=lambda: Bites(
        _browser_release_source(),
        "is cleared to None in",
        at=_BROWSER_RELEASE_OWNER,
    ),
    clean=lambda: (
        # __init__ putting the fields at their initial None is not a release, and neither is the chokepoint's
        # own body.
        Silent(_browser_release_source(), at=_ELSEWHERE),
        f"class R:\n    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):\n        self._browser = None\n",
    ),
)


# --- browser release call sites ----------------------------------------------- A release is a whole-process
# teardown; the methods allowed to trigger one are declared, everything else is a C3.
_BROWSER_RELEASE_CALL_ATTRS = frozenset({"close", "shutdown", "teardown", "dispose"})
_BROWSER_RELEASE_RECEIVERS = frozenset({"driver", "_driver", "browser", "_browser"})
_BROWSER_RELEASE_CALL_EXEMPT = frozenset({"close", "_acquire_browser", "_release_browser"})


def _browser_release_call_offenders(tree, path, *, owners, attrs, receivers, exempt):
    """Flag a browser release call from any method outside the declared owners."""
    if path not in owners:
        return
    for node in functions(tree):
        if node.name in exempt:
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                continue
            callee = inner.func
            receiver = None
            if isinstance(callee.value, ast.Name):
                receiver = callee.value.id
            elif isinstance(callee.value, ast.Attribute):
                receiver = callee.value.attr
            if callee.attr in attrs and receiver in receivers:
                yield offender_at(
                    path,
                    inner,
                    f"{node.name}() releases a browser (.{callee.attr}) outside a declared owner; "
                    f"an attempt lifecycle must not tear down the browser: retry the page",
                )


BROWSER_RELEASE_CALLS_HAVE_DECLARED_OWNERS = Rule(
    name="browser-release-calls-have-declared-owners",
    find=lambda tree, path: _browser_release_call_offenders(
        tree,
        path,
        owners=_BROWSER_RELEASE_OWNERS,
        attrs=_BROWSER_RELEASE_CALL_ATTRS,
        receivers=_BROWSER_RELEASE_RECEIVERS,
        exempt=_BROWSER_RELEASE_CALL_EXEMPT,
    ),
    summary=(
        "A browser release (driver.close() and kin) tears down the whole process, so only "
        "the lifecycle owners may trigger one: a close method delegating shutdown, the "
        "recycle bound in _acquire_browser, and _release_browser itself. A render attempt "
        "is a page lifecycle and must never reach into the browser's"
    ),
    bite=lambda: Bites(
        "class Renderer:\n"
        "    async def render(self):\n"
        "        try:\n"
        "            pass\n"
        "        except Exception:\n"
        "            await self._driver.close()\n",
        "releases a browser",
        at=REPO_ROOT / "crawlutron" / "transport" / "browser.py",
    ),
    clean=lambda: (
        # A close method is a declared owner: it may delegate the teardown.
        "class Renderer:\n    async def close(self):\n        await self._driver.close()\n",
        # The recycle bound releases the old browser to launch a fresh one.
        "class Driver:\n    async def _acquire_browser(self):\n        await self._release_browser()\n",
        # An unrelated close on a foreign receiver is not a browser release.
        "class Handler:\n    async def handle(self):\n        await client.close()\n",
        # The clean half at a path the rule does not own: the sweep is path-keyed.
        Silent(
            "class Renderer:\n"
            "    async def render(self):\n"
            "        try:\n"
            "            pass\n"
            "        except Exception:\n"
            "            await self._driver.close()\n",
            at=_ELSEWHERE,
        ),
    ),
)


#: The lock constructors a store must not hand itself.
_PRIVATE_LOCK_NAMES = frozenset({"Lock", "RLock"})


def _opens_a_store_connection(class_node: ast.ClassDef) -> bool:
    """Whether this class reaches ``connect_store``, the one connection chokepoint.

    Scoping to this call keeps the rule from being wrong: most private locks are
    the correct choice, so only classes reaching the chokepoint are in the set.
    """
    return any(
        isinstance(node, ast.Call)
        and resolved_call_name(class_node, node).rsplit(".", 1)[-1] == "connect_store"
        for node in nodes_of(class_node)
    )


def _private_store_lock_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A store must take the file's process lock, not a lock of its own.

    A per-instance lock excludes nothing, so a second writer runs through a
    migration in flight; the failure is invisible locally, so it must be a gate.
    """
    offenders: list[str] = []
    for class_node in class_defs(tree):
        if not _opens_a_store_connection(class_node):
            continue
        for node in writes(class_node):
            value = node.value
            if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)):
                continue
            if value.func.attr not in _PRIVATE_LOCK_NAMES:
                continue
            if not (isinstance(value.func.value, ast.Name) and value.func.value.id == "threading"):
                continue
            targets = assignment_targets(node)
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_lock":
                    offenders.append(
                        offender_at(
                            path,
                            node,
                            f"{class_node.name} serializes on a private lock, not process_lock",
                        )
                    )
    return offenders


STORES_SERIALIZE_ON_THE_FILES_PROCESS_LOCK = Rule(
    name="stores-serialize-on-the-files-process-lock",
    find=_private_store_lock_offenders,
    summary=(
        "A per-instance threading.Lock excludes that store from no other process, "
        "so a write can run straight through a migration in flight; serialize on "
        "process_lock(store_lock_key(db_path))"
    ),
    proof_path=REPO_ROOT / "crawlutron" / "crawl" / "store.py",
    bite=lambda: (
        Bites(
            "class Store:\n"
            "    def __init__(self, db_path):\n"
            "        self._conn = connect_store(db_path, schema=schema)\n"
            "        self._lock = threading.Lock()\n",
            "private lock, not process_lock",
        ),
        Bites(
            "class Store:\n"
            "    def __init__(self, db_path):\n"
            "        self._conn = connect_store(db_path, schema=schema)\n"
            "        self._lock = threading.RLock()\n",
            "private lock, not process_lock",
        ),
    ),
    clean=lambda: (
        "class Store:\n"
        "    def __init__(self, db_path):\n"
        "        self._lock = process_lock(store_lock_key(db_path))\n"
        "        self._conn = connect_store(db_path, schema=schema)\n",
        # A module-level guard is not a store's write lock, and banning it would be the rule being wrong
        # rather than the code.
        "_PROCESS_LOCKS_GUARD = threading.Lock()\n",
        # State no other process can reach. A private lock is the right answer here, which is the whole reason
        # the rule is scoped to the connection chokepoint rather than banning the constructor.
        "class RobotsCache:\n    def __init__(self):\n        self._lock = threading.Lock()\n",
    ),
)


RULES = Registry(
    (
        BROWSER_RELEASE_ONLY_THROUGH_THE_CHOKEPOINT,
        BROWSER_RELEASE_CALLS_HAVE_DECLARED_OWNERS,
        STORES_SERIALIZE_ON_THE_FILES_PROCESS_LOCK,
    )
)


def test_no_rule_violations():
    """Browser and store state release through their chokepoints."""
    RULES.check_all_clean()
