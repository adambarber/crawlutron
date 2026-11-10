"""S6 contract: the AST parse cache persists source under .pytest_cache/.

parsed() persists each module's source under .pytest_cache/ast_parse/, keyed on
(path, mtime_ns, size, py_version), so repeat sessions reuse it instead of
re-reading the source file. The tree is parsed once per session.
"""

import ast

from tests._walk_primitives import REPO_ROOT, parsed


def test_parsed_persists_source_under_pytest_cache(tmp_path) -> None:
    """A parsed module's source is cached under .pytest_cache/ast_parse/."""
    # Unique per run so a stale cache file from an earlier invocation never matches.
    source = f'S6_PARSE_CACHE_PROBE = "{tmp_path}"\n'
    path = tmp_path / "s6_parse_cache_probe.py"
    path.write_text(source, encoding="utf-8")

    tree = parsed(path)
    assert isinstance(tree, ast.Module)
    targets = [
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    assert targets == ["S6_PARSE_CACHE_PROBE"]

    cache_dir = REPO_ROOT / ".pytest_cache" / "ast_parse"
    matches = [cf for cf in cache_dir.glob("*.src") if cf.read_text(encoding="utf-8") == source]
    assert len(matches) == 1, f"expected exactly one cache file holding the source, found {len(matches)}"

    # The in-memory cache returns the same tree object on repeat reads.
    assert parsed(path) is tree
