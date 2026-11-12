"""Scanners, builders, pollers and constructors are never re-inlined."""

import ast
import contextlib
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    TESTS_DIR,
    call_nodes,
    functions,
    nodes_of,
    offender_at,
    offenders_over_functions,
    trees_excluding,
)
from tests.rules import Bites, Registry, Rule

# --------------------------------------------------------------------------- robots.txt scanning
# ---------------------------------------------------------------------------

_ROBOTS_SCANNER_SANCTIONED = "iter_robots_fields"  # the sanctioned scanner itself, in robots_parse.py


def _robots_scanner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """One line-walk for every robots consumer.

    The ``lstrip(BOM) + splitlines()`` scan lived three times and drifted on the
    empty-value gate; the tokenizer in ``robots_parse.py`` is now the only one.
    """
    offenders: list[str] = []
    for node in functions(tree):
        if node.name == _ROBOTS_SCANNER_SANCTIONED:
            continue
        has_bom_strip = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "attr", "") == "lstrip"
            and s.args
            and isinstance(s.args[0], ast.Constant)
            and s.args[0].value in ("﻿", "\ufeff")
            for s in nodes_of(node)
        )
        has_split = any(
            isinstance(s, ast.Call) and getattr(s.func, "attr", "") == "splitlines" for s in nodes_of(node)
        )
        if has_bom_strip and has_split:
            offenders.append(offender_at(path, node, f"{node.name} re-rolls the robots line-scanner"))
    return offenders


ROBOTS_LINE_SCANNER_LIVES_IN_ONE_PLACE = Rule(
    name="robots-line-scanner-lives-in-one-place",
    find=_robots_scanner_offenders,
    summary=(
        "packages/core/crawlutron/transport/robots_parse.iter_robots_fields is the single scanner for robots"
        " consumers, so the shared line-walk cannot drift on the empty-value gate"
    ),
    bite=lambda: Bites(
        "def parse_robots_rules(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw\n",
        "re-rolls the robots line-scanner",
    ),
    clean=lambda: (
        "def iter_robots_fields(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw.strip()\n",
    ),
    proof_path=APP_ROOT / "crawl" / "_synthetic.py",
)


# Test-fake construction: sanctioned builders over hand-built fakes
# owners: the sanctioned builder modules themselves
_FAKE_BUILDER_SANCTIONED = frozenset({"_fetcher_factories.py", "_fetch_doubles.py"})


def _fetcher_factory_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a test file hand-building a model the fetcher factory already fabricates.

    A fixture spelling ``HttpFetchResult`` by hand silently diverges when the
    contract grows -- exactly the drift this detector exists to catch.
    """
    offenders: list[str] = []
    for node in call_nodes(tree):
        if getattr(node.func, "id", "") == "HttpFetchResult":
            offenders.append(
                offender_at(path, node, "use _fetch_doubles.make_fetch instead of HttpFetchResult(...)")
            )
    return offenders


FETCH_FAKES_COME_FROM_THE_SANCTIONED_BUILDER = Rule(
    name="fetch-fakes-come-from-the-sanctioned-builder",
    find=_fetcher_factory_offenders,
    summary="Use _fetch_doubles.make_fetch()",
    excludes=_FAKE_BUILDER_SANCTIONED,
    bite=lambda: Bites(
        "def f():\n    return HttpFetchResult(url='u')\n",
        "make_fetch",
    ),
    clean=lambda: ("def f():\n    return make_fetch(url='u')\n",),
    scope=lambda *, exclude: trees_excluding(TESTS_DIR, *exclude),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def _call_node_names(node: ast.AST) -> set[str]:
    """Names of every call target under ``node`` (e.g. ``pytest.fail``)."""
    names: set[str] = set()
    for child in nodes_of(node):
        if isinstance(child, ast.Call):
            with contextlib.suppress(Exception):
                names.add(ast.unparse(child.func))
    return names


def _poll_loop_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    def check(node):
        src = ast.dump(node)
        has_while_true = any(
            isinstance(n, ast.While) and ast.unparse(n.test) == "True" for n in nodes_of(node)
        )
        if not (has_while_true and "iscoroutine" in src and "wait_for" in src):
            return []
        # The sanctioned _assertions.poll_until terminates with pytest.fail. A docstring may *claim* a fail;
        # only a real call node counts.
        if any(name == "fail" or name.endswith(".fail") for name in _call_node_names(node)):
            return []
        return [offender_at(path, node, "use _assertions.poll_until instead of a hand-rolled poll loop")]

    return offenders_over_functions(tree, check)


TESTS_USE_THE_SANCTIONED_POLLER = Rule(
    name="tests-use-the-sanctioned-poller",
    find=_poll_loop_offenders,
    summary="Use _assertions.poll_until",
    excludes=_FAKE_BUILDER_SANCTIONED,
    bite=lambda: Bites(
        "async def _poll_until(pred, *, timeout=10.0):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            v = pred()\n"
        "            if asyncio.iscoroutine(v):\n"
        "                v = await v\n"
        "            if v:\n"
        "                return True\n"
        "    return await asyncio.wait_for(_runner(), timeout=timeout)\n",
        "poll_until",
    ),
    clean=lambda: (
        "async def poll_until(pred, *, timeout=10.0, message=None):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            if pred():\n"
        "                return True\n"
        "    await asyncio.wait_for(_runner(), timeout=timeout)\n"
        "    pytest.fail(message)\n",
    ),
    scope=lambda *, exclude: trees_excluding(TESTS_DIR, *exclude),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


# --------------------------------------------------------------------------- dataclass copying: replace, not
# asdict-and-rebuild ---------------------------------------------------------------------------


def _is_asdict_call(node: ast.expr) -> bool:
    """Whether a node calls asdict, under either import spelling.

    ``from dataclasses import asdict`` and ``import dataclasses`` are both in
    use, so a detector keyed on one spelling silently stops detecting the other.
    """
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "asdict")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "asdict")
    )


def _asdict_rebuild_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag unpacking ``asdict(...)`` into a constructor call."""
    offenders: list[str] = []
    for call in call_nodes(tree):
        for keyword in call.keywords:
            if keyword.arg is not None or not isinstance(keyword.value, ast.AST):
                continue
            unpacked = [n for n in call_nodes(keyword.value) if _is_asdict_call(n)]
            if unpacked:
                offenders.append(
                    offender_at(
                        path,
                        unpacked[0],
                        "asdict(...) unpacked into a "
                        "constructor call: the nested dataclasses become dicts and "
                        "the rebuilt object violates its declared field types -- use dataclasses.replace()",
                    )
                )
                break
    return offenders


ASDICT_IS_NEVER_UNPACKED_INTO_A_CONSTRUCTOR = Rule(
    name="asdict-is-never-unpacked-into-a-constructor",
    find=_asdict_rebuild_offenders,
    summary="copy dataclasses with dataclasses.replace(), not asdict(**...)",
    bite=lambda: (
        Bites(
            "import dataclasses\ndef f(r):\n    return Thing(**{**dataclasses.asdict(r), 'd': 1})\n",
            "dataclasses.replace",
        ),
        Bites(
            "from dataclasses import asdict\ndef f(r):\n    return Thing(**asdict(r))\n",
            "dataclasses.replace",
        ),
    ),
    clean=lambda: (
        # Serialization keeps asdict: a dict result used as a dict is its real job.
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    return json.dumps(data)\n",
        "import dataclasses\ndef f(s):\n    return dataclasses.asdict(s)['results']\n",
        "import dataclasses\ndef f(s):\n    data = dataclasses.asdict(s)\n    data['x'] = 1\n    "
        "return data\n",
    ),
    proof_path=APP_ROOT / "_synthetic.py",
)


RULES = Registry(
    (
        ROBOTS_LINE_SCANNER_LIVES_IN_ONE_PLACE,
        FETCH_FAKES_COME_FROM_THE_SANCTIONED_BUILDER,
        TESTS_USE_THE_SANCTIONED_POLLER,
        ASDICT_IS_NEVER_UNPACKED_INTO_A_CONSTRUCTOR,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()
