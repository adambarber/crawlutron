"""Extract ``path:line:text`` comments, or render touched files as code fences.

Usage: ``python scripts/extract_comments.py [-o OUT] [--touched REV] [--grep RE]``;
``--touched`` emits whole files verbatim in tagged fences, ignoring ``--grep``.
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

from utils.paths import REPO_ROOT

# Extensions whose comments are whole-line '#' prefixed. Extensions rather
# than a filename list, so a new .sh script is covered without editing this.
LINE_COMMENT_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".example"}
)
LINE_COMMENT_NAMES = frozenset({"Dockerfile", "Makefile", "Jenkinsfile"})

# Fence languages for the touched renderer; an unlisted suffix emits a bare fence.
LANGUAGE_TAGS = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
}

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


def _git_output(args: list[str]) -> bytes | None:
    """The one git invocation site behind both derived file lists."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout


def _tracked_files() -> list[pathlib.Path]:
    """Every tracked file, or a rglob fallback when git cannot answer.

    Honouring git rather than a hand-maintained skip list keeps scratch dirs out
    of the report; git is optional, so a missing git takes the fallback, not raise.
    """
    output = _git_output(["ls-files", "-z"])
    if output is not None:
        return [REPO_ROOT / name for name in output.decode().split("\0") if name]
    return sorted(
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file() and not FALLBACK_SKIP.intersection(path.relative_to(REPO_ROOT).parts)
    )


def _touched_files(rev: str) -> list[pathlib.Path]:
    """Files changed between ``rev`` and HEAD; empty when git cannot answer."""
    range_spec = rev if ".." in rev else f"{rev}..HEAD"
    output = _git_output(["diff", "--name-only", range_spec])
    if output is None:
        return []
    return [REPO_ROOT / name for name in output.decode().splitlines() if name]


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
    return [node for node in ast.walk(tree) if isinstance(node, ast.Expr) and _is_docstring_node(node)]


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
        if isinstance(holder, ast.Module):
            kind = "module"
        else:
            kind = holder.name
        over.append((doc_node.lineno, doc_node.end_lineno or doc_node.lineno, kind))
    return sorted(over)


def _docstring_records(tree: ast.Module, lines: list[str]) -> list[tuple[int, int, str]]:
    """One record per docstring source line, from the same walk the gate uses."""
    records: list[tuple[int, int, str]] = []
    for node in _docstring_nodes(tree):
        for offset, line in enumerate(lines[node.lineno - 1 : node.end_lineno]):
            records.append((node.lineno + offset, -1, line.rstrip()))
    return records


def _read_source(path: pathlib.Path, *, errors: str = "strict") -> str:
    """The one whole-file read, byte-faithful for UTF-8 (no newline translation)."""
    return path.read_bytes().decode("utf-8", errors=errors)


def _python_records(path: pathlib.Path, *, include_docstrings: bool) -> Iterator[tuple[int, int, str]]:
    """Yield ``(row, col, text)`` for Python comments and docstrings.

    ``tokenize`` rather than a regex, so a ``#`` inside a string is not a comment;
    docstrings come from the AST, which the tokenizer cannot tell from data.
    """
    source = _read_source(path)
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
    for row, line in enumerate(_read_source(path, errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            yield row, 0, stripped


def _records(path: pathlib.Path, *, include_docstrings: bool) -> Iterator[tuple[int, int, str]]:
    if path.suffix == ".py":
        return _python_records(path, include_docstrings=include_docstrings)
    return _text_records(path)


def _emitted(
    paths: list[pathlib.Path],
    *,
    prefixes: tuple[str, ...],
    matcher: re.Pattern[str] | None,
    include_docstrings: bool,
) -> tuple[list[str], dict[str, int], int]:
    """Records, per-extension counts and scanned count for one derived file list."""
    lines: list[str] = []
    counts: dict[str, int] = {}
    scanned = 0
    for path in paths:
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
    return lines, counts, scanned


def _render_touched(paths: list[pathlib.Path]) -> tuple[str, int, int]:
    """Fenced whole-file blocks, rendered count and scanned count; skips missing files."""
    blocks: list[str] = []
    rendered = 0
    scanned = 0
    for path in paths:
        relative = str(path.relative_to(REPO_ROOT))
        if not path.is_file():
            continue
        try:
            content = _read_source(path)
        except (OSError, UnicodeDecodeError) as error:
            print(f"skipping {relative}: {error}", file=sys.stderr)
            continue
        scanned += 1
        rendered += 1
        block = content if content.endswith("\n") else f"{content}\n"
        tag = LANGUAGE_TAGS.get(path.suffix, "")
        blocks.append(f"{relative}\n```{tag}\n{block}```\n")
    return "".join(blocks), rendered, scanned


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
    parser.add_argument(
        "--touched",
        metavar="REV",
        help="render each touched file as a whole-file code fence",
    )
    args = parser.parse_args(argv)

    touched = args.touched is not None
    if touched:
        body, rendered, scanned = _render_touched(_touched_files(args.touched))
        detail = f"{rendered} rendered"
    else:
        matcher = re.compile(args.grep) if args.grep else None
        lines, counts, scanned = _emitted(
            _tracked_files(),
            prefixes=tuple(args.path),
            matcher=matcher,
            include_docstrings=not args.no_docstrings,
        )
        if args.codes:
            body = "\n".join(f"{suffix or 'other':>10}  {count}" for suffix, count in sorted(counts.items()))
        else:
            body = "\n".join(lines)
        if body:
            body += "\n"
        detail = f"{sum(counts.values())} comments, {len(lines)} emitted"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)

    print(f"{scanned} files, {detail} -> {args.output or 'stdout'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
