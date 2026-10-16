"""Module dependency graph and coupling report for app/.

Usage: ``python scripts/module_graph.py [--mermaid] [-o docs/module-graph.md]``. For the
splitting programme: size = work, fan-in = risk, cycles = misplaced boundary; ranked output.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections import defaultdict

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"
STDLIB = set(sys.stdlib_module_names)


def _module_name(path: pathlib.Path) -> str:
    relative = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(name: str) -> str | None:
    """Map an imported name to a module file, or None if it is not ours."""
    root = name.split(".")[0]
    if root in STDLIB:
        return None
    if not (APP_ROOT.parent / name.replace(".", "/")).with_suffix(".py").exists():
        return None
    return name


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (resolved := _resolve(alias.name)) is not None:
                    found.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module and (resolved := _resolve(node.module)) is not None:
                found.add(resolved)
    return found


def build_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    modules: dict[str, set[str]] = {}
    importers: dict[str, set[str]] = defaultdict(set)
    for path in sorted(APP_ROOT.rglob("*.py")):
        name = _module_name(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            print(f"skipping {name}: {error}", file=sys.stderr)
            continue
        modules[name] = _imports(tree)
    for name, targets in modules.items():
        for target in targets:
            importers[target].add(name)
    return modules, importers


def cycles(modules: dict[str, set[str]]) -> list[list[str]]:
    """Every elementary cycle, via DFS colouring. Small graph, so exhaustive is fine."""
    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    stack: list[str] = []

    def walk(node: str) -> None:
        if node in stack:
            cycle = stack[stack.index(node) :]
            key = tuple(sorted(cycle))
            if key not in seen:
                seen.add(key)
                found.append([*cycle, node])
            return
        stack.append(node)
        for target in sorted(modules.get(node, ())):
            walk(target)
        stack.pop()

    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(modules) * 2))
    for name in sorted(modules):
        walk(name)
    return found


def report(modules: dict[str, set[str]], importers: dict[str, set[str]]) -> None:
    print(f"{len(modules)} modules, {sum(len(t) for t in modules.values())} internal edges\n")
    ranked_in = sorted(modules, key=lambda name: (-len(importers.get(name, ())), name))
    print("FAN-IN (hubs -- every one is a caller to update when this module changes)")
    for name in ranked_in[:15]:
        print(f"  {len(importers.get(name, ())):>3}  {name}")
    ranked_out = sorted(modules, key=lambda name: (-len(modules[name]), name))
    print("\nFAN-OUT (reaching widely -- candidates for a narrower dependency)")
    for name in ranked_out[:10]:
        print(f"  {len(modules[name]):>3}  {name}")
    found = cycles(modules)
    print(f"\nCYCLES: {len(found)}")
    for cycle in found:
        print("  " + " -> ".join(cycle))


def mermaid(modules: dict[str, set[str]]) -> str:
    def node(name: str) -> str:
        return name.replace("app.", "").replace(".", "_").replace("-", "_")

    lines = ["graph LR"]
    for name in sorted(modules):
        for target in sorted(modules[name]):
            lines.append(f"    {node(name)} --> {node(target)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mermaid", action="store_true", help="emit a mermaid diagram")
    parser.add_argument("-o", "--output", type=pathlib.Path, help="write to this file")
    args = parser.parse_args()

    modules, importers = build_graph()
    if args.mermaid:
        text = mermaid(modules)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(f"```mermaid\n{text}\n```\n", encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
    else:
        report(modules, importers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
