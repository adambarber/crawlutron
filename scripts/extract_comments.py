"""Extract every comment in the repository into one file, with file and line.

Usage: ``python scripts/extract_comments.py [-o OUT] [--grep RE] [--no-docstrings] [--path TREE]``
-- one ``path:line:text`` record per line; comment hygiene is a reader's call, not a regex gate.
"""

from __future__ import annotations

import argparse
import ast
import io
import pathlib
import re
import shutil
import subprocess
import sys
import tokenize
from collections.abc import Iterator

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Extensions whose comments are whole-line '#' prefixed. Extensions rather
# than a filename list, so a new .sh script is covered without editing this.
LINE_COMMENT_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".example"}
)
LINE_COMMENT_NAMES = frozenset({"Dockerfile", "Makefile", "Jenkinsfile"})

# Directories that are never part of a tracked tree but can appear when the
# script is run outside a checkout. Only a fallback: git ls-files governs.
FALLBACK_SKIP = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)


def _tracked_files() -> list[pathlib.Path]:
    """Every tracked file, or a rglob fallback when git cannot answer.

    Honouring git rather than a hand-maintained skip list keeps scratch dirs out
    of the report; git is optional, so a missing git takes the fallback, not raise.
    """
    git = shutil.which("git")
    if git is not None:
        try:
            completed = subprocess.run(
                [git, "ls-files", "-z"],
                cwd=REPO_ROOT,
                capture_output=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            return [REPO_ROOT / name for name in completed.stdout.decode().split("\0") if name]
    return sorted(
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file() and not FALLBACK_SKIP.intersection(path.relative_to(REPO_ROOT).parts)
    )


def _is_comment_file(path: pathlib.Path) -> bool:
    return path.suffix in LINE_COMMENT_SUFFIXES or path.name in LINE_COMMENT_NAMES or path.suffix == ".py"


def _is_docstring_node(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.end_lineno is not None
    )


def _docstring_nodes(tree: ast.Module) -> list[ast.Expr]:
    """Every docstring node, from ONE walk.

    The extract's line records and the gate's length decision both consume
    this list, so the walk lives here and nowhere else.
    """
    return [node for node in ast.walk(tree) if _is_docstring_node(node)]


def over_line_docstrings(tree: ast.Module, line_limit: int) -> list[tuple[int, int, str]]:
    """Every docstring longer than ``line_limit`` content lines, from ONE tree.

    Only real docstring positions count -- a string at a module, function or class
    body's head; content lines exclude the quoting.
    """
    holders = [tree] + [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    over: list[tuple[int, int, str]] = []
    for holder in holders:
        text = ast.get_docstring(holder)
        if text is None:
            continue
        content = [line for line in text.splitlines() if line.strip()]
        if len(content) <= line_limit:
            continue
        doc_node = holder.body[0] if holder is not tree else tree.body[0]
        kind = "module" if holder is tree else holder.name
        over.append((doc_node.lineno, doc_node.end_lineno or doc_node.lineno, kind))
    return sorted(over)


def _docstring_records(tree: ast.Module, lines: list[str]) -> list[tuple[int, int, str]]:
    """One record per docstring source line, from the same walk the gate uses."""
    records: list[tuple[int, int, str]] = []
    for node in _docstring_nodes(tree):
        for offset, line in enumerate(lines[node.lineno - 1 : node.end_lineno]):
            records.append((node.lineno + offset, -1, line.rstrip()))
    return records


def _python_records(path: pathlib.Path, *, include_docstrings: bool) -> Iterator[tuple[int, int, str]]:
    """Yield ``(row, col, text)`` for Python comments and docstrings.

    ``tokenize`` rather than a regex, so a ``#`` inside a string is not a comment;
    docstrings come from the AST, which the tokenizer cannot tell from data.
    """
    source = path.read_text(encoding="utf-8")
    records: list[tuple[int, int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                records.append((token.start[0], token.start[1], token.string.strip()))
    except SyntaxError:
        # IndentationError (a SyntaxError subclass, never a TokenError) escapes
        # generate_tokens; it propagates so the caller names the skipped file.
        raise
    if include_docstrings:
        # A file the tokenizer accepted but the parser rejects is not silent:
        # its comments are still reported; docstrings are what can be missed.
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            records.extend(_docstring_records(tree, source.splitlines()))
    records.sort()
    return iter(records)


def _text_records(path: pathlib.Path) -> Iterator[tuple[int, int, str]]:
    """Yield whole-line comments from a non-Python source file.

    Whole-line only: an inline ``#`` in YAML/TOML may sit in a quoted scalar or
    after a URL, and reporting a fragment of a URL as a comment would make the extract lie.
    """
    for row, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            yield row, 0, stripped


def _records(path: pathlib.Path, *, include_docstrings: bool) -> Iterator[tuple[int, int, str]]:
    if path.suffix == ".py":
        return _python_records(path, include_docstrings=include_docstrings)
    return _text_records(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", type=pathlib.Path, help="write the extract here instead of stdout")
    parser.add_argument("--grep", help="only emit lines matching this regex")
    parser.add_argument(
        "--no-docstrings",
        action="store_true",
        help="omit docstrings, leaving only '#' line comments",
    )
    parser.add_argument("--path", action="append", default=[], help="limit to this path prefix (repeatable)")
    parser.add_argument(
        "--codes", action="store_true", help="count by file extension instead of listing records"
    )
    args = parser.parse_args(argv)

    matcher = re.compile(args.grep) if args.grep else None
    prefixes = tuple(args.path)
    include_docstrings = not args.no_docstrings

    lines: list[str] = []
    counts: dict[str, int] = {}
    scanned = 0
    for path in _tracked_files():
        relative = str(path.relative_to(REPO_ROOT))
        if not _is_comment_file(path) or (prefixes and not relative.startswith(prefixes)):
            continue
        try:
            records = list(_records(path, include_docstrings=include_docstrings))
        except (OSError, UnicodeDecodeError, tokenize.TokenError, SyntaxError) as error:
            # SyntaxError here is the tokenizer's (IndentationError escapes
            # generate_tokens): the file is named and skipped, never silent.
            print(f"skipping {relative}: {error}", file=sys.stderr)
            continue
        scanned += 1
        counts[path.suffix or path.name] = counts.get(path.suffix or path.name, 0) + len(records)
        for row, _col, text in records:
            if matcher is not None and not matcher.search(text):
                continue
            lines.append(f"{relative}:{row}:{text}")

    if args.codes:
        body = "\n".join(f"{suffix or 'other':>10}  {count}" for suffix, count in sorted(counts.items()))
    else:
        body = "\n".join(lines)
    if body:
        body += "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)

    print(
        f"{scanned} files, {sum(counts.values())} comments, {len(lines)} emitted -> "
        f"{args.output or 'stdout'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
