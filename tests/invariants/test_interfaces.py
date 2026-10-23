"""What an app class promises, checked against who is listening.

A base class is a promise to a consumer; a promise with no listener is weight
that compiles clean, reads as capability, and never fails a test.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from tests.chokepoint_walks import REPO_ROOT, app_trees, offender_at
from tests.rules import Bites, Rule

# --------------------------------------------------------------------------- Container protocols on app
# classes

_CONTAINER_PROTOCOL_BASES = frozenset(
    {
        "Counter",
        "Mapping",
        "MutableMapping",
        "MutableSequence",
        "MutableSet",
        "OrderedDict",
        "Sequence",
        "Set",
        "defaultdict",
        "deque",
        "dict",
        "list",
    }
)


def _base_class_name(base: ast.expr) -> str | None:
    """The trailing name behind a base, however it was written.

    ``Foo``, ``mod.Foo`` and ``Foo[int]`` all name ``Foo``; only the dotted and
    subscripted forms were unreadable, and app/ already writes bases that way.
    """
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_class_name(base.value)
    return None


def _container_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names that an import aliased onto a container protocol."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.asname and alias.name in _CONTAINER_PROTOCOL_BASES:
                aliases.add(alias.asname)
    return frozenset(aliases)


def _type_position_names(tree: ast.Module) -> frozenset[str]:
    """Names app/ declares in a type position: annotated, bound, or isinstance'd.

    Deliberately not every mention: a class only ever instantiated promises
    nothing, and counting instantiation hid ten unclaimed methods.
    """
    names: set[str] = set()

    def harvest(annotation: ast.expr | None) -> None:
        if annotation is None:
            return
        for node in ast.walk(annotation):
            if isinstance(node, ast.Name):
                names.add(node.id)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            harvest(node.returns)
        elif isinstance(node, ast.arg):
            harvest(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            harvest(node.annotation)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
            for argument in node.args[1:]:
                harvest(argument)
    return frozenset(names)


@lru_cache(maxsize=1)
def _app_announced_names() -> frozenset[str]:
    """Every name app/ declares in a type position anywhere in the tree."""
    announced: set[str] = set()
    for _path, tree in app_trees():
        announced |= _type_position_names(tree)
    return frozenset(announced)


def _container_base_reading_is_not_broken() -> None:
    """The derivation finds a container base when one is there.

    Asserted against a synthetic tree; the only failure it must catch is the
    derivation quietly matching nothing and going inert.
    """
    tree = ast.parse("class _Probe(MutableMapping[str, str]):\n    pass\n")
    declared = {
        _base_class_name(base)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
    }
    assert declared & _CONTAINER_PROTOCOL_BASES, "the container-base derivation matches nothing"
    assert _container_protocol_offenders(tree, REPO_ROOT / "app" / "_probe.py"), "the detector stopped firing"


def _container_protocol_offenders(tree: ast.Module, path: Path) -> list[str]:
    """Classes that promise to behave like a container for nobody in app/.

    A name counts announced if app/ or the swept tree mentions it; a class
    another declaration in the same file annotates is claimed, not orphaned.
    """
    announced = _app_announced_names() | _type_position_names(tree)
    container_names = _CONTAINER_PROTOCOL_BASES | _container_aliases(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        container = sorted({_base_class_name(base) for base in node.bases} & container_names)
        if not container or node.name in announced:
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{node.name} inherits {container[0]} but no app/ signature names it, so every"
                " method it inherits is honoured for a consumer outside app/",
            )
        )
    return offenders


NO_UNCLAIMED_CONTAINER_PROTOCOL = Rule(
    name="no-unclaimed-container-protocol",
    find=_container_protocol_offenders,
    summary=(
        "an app class may inherit a container protocol only when an app-side signature"
        " names it; otherwise the inherited surface exists only for a consumer outside"
        " app/ and is dead weight in production"
    ),
    premise=_container_base_reading_is_not_broken,
    bite=lambda: (
        Bites(
            "from collections.abc import MutableMapping\n\n"
            "class _SyntheticJar(MutableMapping[str, str]):\n"
            "    def __getitem__(self, key):\n"
            "        raise KeyError(key)\n",
            "no app/ signature names it",
        ),
        Bites(
            "from collections import OrderedDict\n\n"
            "class _SyntheticSlots(OrderedDict[str, str]):\n    pass\n",
            "no app/ signature names it",
        ),
        Bites(
            "import collections.abc\n\n"
            "class _SyntheticQualified(collections.abc.MutableMapping[str, str]):\n"
            "    def __getitem__(self, key):\n"
            "        raise KeyError(key)\n",
            "no app/ signature names it",
        ),
        Bites(
            "from collections.abc import MutableMapping as _Renamed\n\n"
            "class _SyntheticAliased(_Renamed[str, str]):\n"
            "    def __getitem__(self, key):\n"
            "        raise KeyError(key)\n",
            "no app/ signature names it",
        ),
    ),
    clean=lambda: (
        "from collections.abc import MutableMapping\n\n"
        "class SyntheticJar(MutableMapping[str, str]):\n"
        "    def use(jar: SyntheticJar) -> int:\n"
        "        return len(jar)\n",
        "class SyntheticChild(SyntheticParent):\n    pass\n",
        "class SyntheticBase(ABC):\n    pass\n",
        "class SyntheticShape(Protocol):\n    def read(self) -> str: ...\n",
        "class SyntheticWorker(threading.Thread):\n    pass\n",
    ),
)


def test_no_app_class_promises_a_container_nobody_asks_for():
    """`_LockedDomainCookieStore` inherited ten methods no app/ caller reaches."""
    NO_UNCLAIMED_CONTAINER_PROTOCOL.scan()
