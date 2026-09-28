"""One-shot: repoint `nodes_of_type(root, <kinds>)` at the concrete walkers.

Call sites first, then the `from tests.chokepoint_walks import (...)` block,
because the import list is only knowable once every call has been rewritten.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path("/Users/adambarber/Projects/crawlutron")
TESTS = ROOT / "tests"
SOURCE = "chokepoint_walks"

MAPPING = {
    ("ast.Call",): "call_nodes",
    ("ast.Attribute",): "attribute_reads",
    ("ast.Name",): "name_reads",
    ("ast.Constant",): "string_literals",
    ("ast.Subscript",): "subscript_reads",
    ("ast.Dict",): "dict_displays",
    ("ast.ClassDef",): "class_defs",
    ("ast.Try",): "try_blocks",
    ("ast.Return",): "returns",
    ("ast.UnaryOp",): "unary_ops",
    ("ast.JoinedStr",): "f_strings",
    ("ast.FunctionDef", "ast.AsyncFunctionDef"): "functions",
    ("ast.FunctionDef", "ast.AsyncFunctionDef", "ast.Lambda"): "callable_defs",
    ("ast.Assign", "ast.AnnAssign"): "writes",
    ("ast.Assign", "ast.AnnAssign", "ast.AugAssign"): "writes",
    ("ast.Import", "ast.ImportFrom"): "import_statements",
    ("ast.With", "ast.AsyncWith"): "with_blocks",
    ("ast.ListComp", "ast.SetComp", "ast.GeneratorExp"): "comprehensions",
}

CALL = re.compile(r"nodes_of_type\(\s*([A-Za-z_][\w.]*)\s*,\s*([^)]*?)\s*\)")
ARGS = re.compile(r"ast\.(\w+)")


def key_of(kinds: list[str]) -> tuple[str, ...]:
    return tuple(sorted(ast.unparse(ast.parse(f"ast.{k}")) for k in kinds))


def rewrite(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "nodes_of_type" not in text:
        return False

    used: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        walker = MAPPING[key_of(ARGS.findall(m.group(2)))]
        used.add(walker)
        return f"{walker}({m.group(1)})"

    out = CALL.sub(sub, text)

    # Import block: `nodes_of_type` is gone, so the names it carried go with it
    # and the concrete walkers this file actually uses take their place.
    def fix(m: re.Match[str]) -> str:
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        if "nodes_of_type" not in names:
            return m.group(0)
        names = [n for n in names if n != "nodes_of_type"]
        names = sorted({*names, *used})
        return f"from {SOURCE} import (\n" + "".join(f"    {n},\n" for n in names) + ")"

    out = re.sub(rf"from {SOURCE} import \(([^)]*)\)", fix, out)
    ast.parse(out)
    path.write_text(out, encoding="utf-8")
    print(f"{path.relative_to(ROOT)}: repointed ({', '.join(sorted(used)) or 'import only'})")
    return True


for p in sorted(TESTS.rglob("*.py")):
    if SOURCE in p.name:
        continue
    rewrite(p)

# The source module's own remaining references, if any.
src = TESTS / f"{SOURCE}.py"
leftover = [i for i, ln in enumerate(src.read_text(encoding="utf-8").splitlines(), 1) if "nodes_of_type" in ln]
if leftover:
    sys.exit(f"nodes_of_type still in {src.name} at {leftover}")
print("chokepoint_walks.py: clean")
