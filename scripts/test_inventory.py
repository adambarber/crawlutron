"""Test inventory: every test, its claimed behavior, and its object under test.

Walks the tests surface and infers each test's production object from imports
and call targets, grouping by claimed behavior and flagging duplication or coupling.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys
from collections import defaultdict

from utils.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT
_root_on_path = str(_REPO_ROOT) in sys.path
if not _root_on_path:
    sys.path.insert(0, str(_REPO_ROOT))


def production_import_names() -> frozenset[str]:
    """Top-level import names of the production packages, derived from uv members."""
    from tests._walk_primitives import import_roots

    return frozenset(root.rsplit("/", 1)[-1] for root in import_roots())


PRODUCTION = production_import_names()


def is_production(name: str) -> bool:
    """True when a dotted name's top segment is a production package."""
    return name.split(".", 1)[0] in PRODUCTION


def test_node_id(relative: str, class_name: str | None, test_name: str) -> str:
    """Build a pytest-style node id from relative path, optional class, test name."""
    parts = [relative]
    if class_name is not None:
        parts.append(class_name)
    parts.append(test_name)
    return "::".join(parts)


def claimed_behavior(node: ast.FunctionDef | ast.AsyncFunctionDef, test_name: str) -> str:
    """Docstring first, falling back to a readable form of the test name."""
    doc = ast.get_docstring(node)
    if doc:
        return doc.strip()
    return test_name[len("test_") :].replace("_", " ").strip()


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """Local name -> production dotted object bound by module-level imports."""
    from tests.chokepoint_walks import from_imports, plain_imports

    bindings: dict[str, str] = {}
    for from_imp in from_imports(tree):
        if from_imp.module and is_production(from_imp.module):
            for alias in from_imp.names:
                local = alias.asname or alias.name
                bindings[local] = f"{from_imp.module}.{alias.name}"
    for import_stmt in plain_imports(tree):
        for alias in import_stmt.names:
            top = alias.name.split(".", 1)[0]
            if top in PRODUCTION:
                local = alias.asname or top
                bindings[local] = alias.name
    return bindings


def _call_objects(tree: ast.Module, node: ast.AST) -> set[str]:
    """Production dotted names reached by calls inside ``node``'s body only."""
    from tests.chokepoint_walks import call_nodes, resolved_call_name

    found: set[str] = set()
    for call in call_nodes(node):
        resolved = resolved_call_name(tree, call)
        if resolved and is_production(resolved):
            found.add(resolved)
    return found


def collect_tests() -> list[dict]:
    """Every test function: module-level and inside Test* classes."""
    from tests._walk_primitives import get_test_roots as test_roots
    from tests._walk_primitives import trees_under
    from tests.chokepoint_walks import class_defs, functions

    tests: list[dict] = []
    for root in test_roots():
        directory = REPO_ROOT / root
        for path, tree in trees_under(directory):
            relative = path.relative_to(REPO_ROOT).as_posix()
            import_bindings = _import_bindings(tree)
            candidates: list[tuple[str | None, ast.FunctionDef | ast.AsyncFunctionDef]] = []
            class_enclosed: set[int] = set()
            for cls in class_defs(tree):
                if not cls.name.startswith("Test"):
                    continue
                for item in cls.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                        "test_"
                    ):
                        class_enclosed.add(id(item))
                        candidates.append((cls.name, item))
            for fn in functions(tree):
                if fn.name.startswith("test_") and id(fn) not in class_enclosed:
                    candidates.append((None, fn))
            for class_name, fn in candidates:
                tests.append(_test_record(path, relative, tree, class_name, fn, import_bindings))
    tests.sort(key=lambda t: (t["file"], t["line"], t["test"]))
    return tests


def _test_record(
    path: pathlib.Path,
    relative: str,
    tree: ast.Module,
    class_name: str | None,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    import_bindings: dict[str, str],
) -> dict[str, object]:
    """One test's inventory record.

    Precedence: called > imported > unresolved; ties break by (length, name).
    """
    from tests.chokepoint_walks import name_reads

    call_objects = _call_objects(tree, fn)
    read_names = {node.id for node in name_reads(fn)}
    test_imports = {import_bindings[name] for name in read_names if name in import_bindings}
    if call_objects:
        primary = sorted(call_objects, key=lambda o: (len(o), o))[-1]
    elif test_imports:
        primary = sorted(test_imports, key=lambda o: (len(o), o))[-1]
    else:
        primary = "unresolved"
    objects = test_imports | call_objects
    return {
        "node_id": test_node_id(relative, class_name, fn.name),
        "file": relative,
        "line": fn.lineno,
        "class": class_name,
        "test": fn.name,
        "behavior": claimed_behavior(fn, fn.name),
        "object": primary,
        "objects": sorted(objects),
    }


def build_groups(tests: list[dict]) -> dict[str, list[dict]]:
    """Group tests by claimed behavior, sorted within each group."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tests:
        groups[t["behavior"]].append(t)
    for group in groups.values():
        group.sort(key=lambda t: (t["file"], t["line"], t["test"]))
    return dict(sorted(groups.items()))


def duplication_flags(groups: dict[str, list[dict]]) -> list[dict]:
    """Same behavior claimed by >1 test on the same object."""
    flags: list[dict] = []
    for behavior, group in groups.items():
        by_object: dict[str, list[dict]] = defaultdict(list)
        for t in group:
            by_object[t["object"]].append(t)
        for object_name, members in by_object.items():
            if len(members) > 1:
                flags.append(
                    {
                        "behavior": behavior,
                        "object": object_name,
                        "tests": [m["node_id"] for m in members],
                    }
                )
    flags.sort(key=lambda f: (f["behavior"], f["object"]))
    return flags


def coupling_flags(tests: list[dict], limit: int = 5) -> list[dict]:
    """One test touching more than ``limit`` distinct production objects."""
    flags = [t for t in tests if len(t["objects"]) > limit]
    flags.sort(key=lambda t: (-len(t["objects"]), t["file"], t["line"]))
    return flags


def fragmentation_flags(groups: dict[str, list[dict]]) -> list[dict]:
    """Same object tested under more than one behavior claim."""
    by_object: dict[str, set[str]] = defaultdict(set)
    for behavior, group in groups.items():
        for t in group:
            by_object[t["object"]].add(behavior)
    flags = [
        {"object": obj, "behaviors": sorted(behaviors)}
        for obj, behaviors in sorted(by_object.items())
        if len(behaviors) > 1
    ]
    return flags


def render(
    tests: list[dict],
    groups: dict[str, list[dict]],
    duplication: list[dict],
    coupling: list[dict],
    fragmentation: list[dict],
    fmt: str,
) -> str:
    """Render the inventory report as text or JSON."""
    if fmt == "json":
        payload = {
            "total": len(tests),
            "groups": [{"behavior": behavior, "tests": group} for behavior, group in groups.items()],
            "duplication": duplication,
            "fragmentation": fragmentation,
            "coupling": [
                {"node_id": t["node_id"], "objects": t["objects"], "count": len(t["objects"])}
                for t in coupling
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    lines: list[str] = []
    lines.append(f"total tests: {len(tests)}")
    lines.append("")
    lines.append("behavior groups:")
    for behavior, group in groups.items():
        count = len(group)
        lines.append(f"  - {behavior}  ({count} test{'s' if count != 1 else ''})")
        for t in group:
            lines.append(f"      {t['node_id']}  object={t['object']}")
    lines.append("")
    if duplication:
        lines.append("duplication flags (same behavior, same object, >1 test):")
        for f in duplication:
            lines.append(f"  - {f['behavior']}  object={f['object']}")
            for node_id in f["tests"]:
                lines.append(f"      {node_id}")
        lines.append("")
    if fragmentation:
        lines.append("fragmentation flags (same object, >1 behavior claim):")
        for f in fragmentation:
            lines.append(f"  - object={f['object']}")
            for b in f["behaviors"]:
                lines.append(f"      {b}")
        lines.append("")
    if coupling:
        lines.append("tight coupling flags (>5 distinct production objects per test):")
        for t in coupling:
            lines.append(f"  - {t['node_id']}  objects={len(t['objects'])}")
            for obj in t["objects"]:
                lines.append(f"      {obj}")
        lines.append("")
    lines.append(
        f"summary: {len(tests)} tests, {len(groups)} behaviors, "
        f"{len(duplication)} duplication, {len(fragmentation)} fragmentation, "
        f"{len(coupling)} coupling"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    """Parse args, run the inventory, write text or JSON, always exit 0."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--out", type=pathlib.Path, help="write report to this file")
    args = parser.parse_args()

    try:
        tests = collect_tests()
        groups = build_groups(tests)
        duplication = duplication_flags(groups)
        fragmentation = fragmentation_flags(groups)
        coupling = coupling_flags(tests)
        report = render(tests, groups, duplication, coupling, fragmentation, args.format)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(report, end="")
    except Exception as error:  # analysis tool: never fail the run
        print(f"test_inventory: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
