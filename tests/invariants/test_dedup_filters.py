"""Filters, pattern caches and comment blocks have one home."""

import ast
import io
import pathlib
import tokenize
from collections.abc import Iterable
from pathlib import Path

from tests.chokepoint_walks import (
    REPO_ROOT,
    app_python_files,
    calls_named,
    comprehensions,
    offender_at,
    relative_posix,
)
from tests.invariants._shared import _enclosing_defs
from tests.rules import Bites, Registry, Rule, Silent

_JS_ENDPOINTS_FILTER_SANCTIONED = frozenset({"validation.py"})


def _reinline_js_endpoints_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the inlined endpoint-filter comprehension outside its chokepoint.

    The reinlined shape is a ``{**endpoint, "url": clean}`` comprehension -- a
    rewritten ``url`` key is what makes it an endpoint filter, not a copy.
    """
    if path.name == "validation.py":
        return []
    offenders: list[str] = []
    for node in comprehensions(tree):
        elt = node.elt
        if not isinstance(elt, ast.Dict):
            continue
        if any(k is None for k in elt.keys) and any(
            isinstance(k, ast.Constant) and k.value == "url" for k in elt.keys
        ):
            offenders.append(
                offender_at(path, node, "inlined js_endpoints filter: use validation.filter_js_endpoints")
            )
    return offenders


_JS_EGRESS_ELSEWHERE = REPO_ROOT / "app" / "transport" / "content_cache.py"


# The chokepoint's own comprehension really is the matched shape: the file exemption is what keeps the real
# tree clean, not a predicate that misses it.
_JS_ENDPOINTS_VALIDATION_SOURCE = (
    "def filter_js_endpoints(endpoints, *, resolve_against=None, policy=None):\n"
    "    return [\n"
    "        {**endpoint, 'url': clean.raw}\n"
    "        for endpoint in endpoints\n"
    "        if (clean := admit_or_none(endpoint)) is not None\n"
    "    ]\n"
)


JS_ENDPOINTS_FILTER_IS_NOT_REINLINED = Rule(
    name="js-endpoints-filter-is-not-reinlined",
    find=_reinline_js_endpoints_filter_offenders,
    summary="route endpoints through filter_js_endpoints",
    excludes=frozenset({"validation.py"}),
    bite=lambda: (
        Bites(
            "def f(endpoints, base):\n"
            '    return [{**e, "url": c} for e in endpoints\n'
            '           if (c := admit_or_none(e["url"])) is not None]\n',
            "filter_js_endpoints",
        ),
        # The detector genuinely matches the chokepoint's OWN comprehension too: the exemption is the path,
        # not a predicate that happens to miss it.
        Bites(
            _JS_ENDPOINTS_VALIDATION_SOURCE,
            "filter_js_endpoints",
            at=_JS_EGRESS_ELSEWHERE,
        ),
    ),
    clean=lambda: (
        "def f(endpoints, base):\n    return filter_js_endpoints(endpoints, resolve_against=base)\n",
        # The very same chokepoint comprehension stays clean at its own path: validation.py is where the
        # filter legitimately lives.
        Silent(_JS_ENDPOINTS_VALIDATION_SOURCE, at=REPO_ROOT / "app" / "safety" / "validation.py"),
    ),
)


# --- 4a: the robots matcher compiler is reachable only via its host cache ----

_ROBOTS_COMPILE = "_compile_robots_pattern"


_ROBOTS_COMPILE_HOME = "_RobotsPatternCache"


def _robots_compile_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_compile_robots_pattern`` may be called only from ``_RobotsPatternCache.get``.

    A second caller reintroduces a process-global matcher cache keyed only on the
    pattern string -- so a hostile site spraying patterns controls the whole cache.
    """
    offenders: list[str] = []
    for node in calls_named(tree, _ROBOTS_COMPILE):
        scopes = _enclosing_defs(node, tree)
        if _ROBOTS_COMPILE_HOME in scopes and "get" in scopes[: scopes.index(_ROBOTS_COMPILE_HOME) + 1]:
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{_ROBOTS_COMPILE} called outside {_ROBOTS_COMPILE_HOME}.get; route through "
                f"the per-host cache so a hostile robots.txt cannot evict another site's matchers",
            )
        )
    return offenders


ROBOTS_PATTERN_COMPILE_LIVES_BEHIND_THE_PER_HOST_CACHE = Rule(
    name="robots-pattern-compile-lives-behind-the-per-host-cache",
    find=_robots_compile_call_offenders,
    summary=(
        "Every caller reaches the pattern compiler through _RobotsPatternCache.get, "
        "so a hostile robots.txt spraying distinct patterns cannot evict another "
        "site's matchers from a global cache"
    ),
    bite=lambda: (
        # A direct caller -- exactly the shape that reintroduces a global, attacker-keyed matcher cache.
        Bites(
            "def f(pattern):\n    return _compile_robots_pattern(pattern)\n",
            "per-host cache",
        ),
        # A same-named method on any OTHER class is not the home.
        Bites(
            "class OtherCache:\n    def get(self, pattern):\n"
            "        return _compile_robots_pattern(pattern)\n",
            "per-host cache",
        ),
    ),
    clean=lambda: (
        # The sanctioned home passes.
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        return _compile_robots_pattern(pattern)\n",
        # A nested helper inside the home class is still inside the home scope.
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        def build():\n"
        "            return _compile_robots_pattern(pattern)\n"
        "        return build()\n",
    ),
    proof_path=REPO_ROOT / "app" / "_synthetic.py",
)

_COMMENT_CLONE_MIN_LINES = 3


# Emitted after every comment-only line, and around indentation: none of them says anything about prose, so
# only a token that carries code ends a block.
_COMMENT_TRANSPARENT = frozenset(
    {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
)


def _comment_blocks(source: str) -> list[tuple[int, tuple[str, ...]]]:
    """``(first line, normalized text)`` for each run of consecutive comment lines.

    ``tokenize`` rather than a ``#`` search (a ``#`` in a string is not a
    comment); a blank line or statement ends a block rather than welding prose.
    """
    blocks: list[tuple[int, tuple[str, ...]]] = []
    lines: list[str] = []
    first_line = 0
    previous_end = 0

    def flush() -> None:
        nonlocal lines, first_line
        if lines:
            blocks.append((first_line, tuple(lines)))
            lines = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            text = " ".join(token.string.lstrip("#").split())
            if not text:
                continue
            if lines and token.start[0] != previous_end + 1:
                flush()
            if not lines:
                first_line = token.start[0]
            lines.append(text)
            previous_end = token.end[0]
        elif token.type not in _COMMENT_TRANSPARENT:
            flush()
    flush()
    return blocks


def _duplicated_comment_runs(
    sources: Iterable[tuple[Path, str]], *, minimum: int = _COMMENT_CLONE_MIN_LINES
) -> list[str]:
    """Comment blocks written twice in app/, with every site that repeats them.

    jscpd matches tokens, so a duplicated six-line WHY reads as zero clones and
    two copies can drift; comparison is whitespace-normalized, so re-indent matches.
    """
    sites: dict[tuple[str, ...], list[tuple[Path, int]]] = {}
    for path, source in sources:
        for line, block in _comment_blocks(source):
            if len(block) < minimum:
                continue
            sites.setdefault(block, []).append((path, line))
    return [
        " and ".join(f"{relative_posix(path)}:{line}" for path, line in found)
        + f" carry the same {len(block)}-line comment: {block[0]}"
        for block, found in sorted(sites.items())
        if len(found) > 1
    ]


def test_no_comment_block_is_written_twice():
    """app/ states each multi-line WHY once, not once per call site."""
    offenders = _duplicated_comment_runs(
        (path, path.read_text(encoding="utf-8")) for path in app_python_files(exclude=frozenset())
    )
    assert not offenders, "comment blocks written more than once: " + ", ".join(offenders)


def test_comment_clone_detector_bites():
    """A repeated block fires, in one file and across two; distinct blocks do not.

    Hand-rolled rather than ``assert_detector`` (which parses to modules), because
    a comment is not in the ``ast`` -- this detector reads the text.
    """
    path = REPO_ROOT / "app" / "transport" / "content_cache.py"
    repeated = "# first line of the note\n# second line\n# third line\n"
    other = "# a different note entirely\n# with three lines\n# of its own text\n"
    detached = "        # first line of the note\n        # second line\n        # third line\n"

    def detect(source: str) -> list[str]:
        return _duplicated_comment_runs([(path, source)])

    once = detect(f"x = 1\n{repeated}y = 2\n")
    assert not once, once
    twice = detect(f"x = 1\n{repeated}y = 2\n{repeated}")
    expected = "app/transport/content_cache.py:2 and app/transport/content_cache.py:6"
    assert len(twice) == 1 and expected in twice[0], twice
    # Two blocks of the same length but different text are two notes.
    assert not detect(f"x = 1\n{repeated}y = 2\n{other}"), "equal-length distinct blocks are not clones"
    # Two lines is a section rule, not a WHY to centralize.
    two_lines = "# first line of the note\n# second line\n"
    assert not detect(f"x = 1\n{two_lines}y = 2\n{two_lines}"), "a two-line rule is below the floor"
    # A blank line ends a block, so two paragraphs stay two paragraphs instead of one welded run -- which
    # would match nothing, including its own twin.
    assert not detect(f"{repeated}\n{other}"), "a blank line must end a comment block"
    assert [line for line, _ in _comment_blocks(f"{repeated}\n{other}")] == [1, 5]
    # Identical prose is still a duplicate across a blank line: the rule about blocks decides what counts as
    # one note, not whether prose may repeat.
    assert detect(f"{repeated}\n{repeated}"), "identical prose is a duplicate however it is separated"
    # Normalized on whitespace, so a re-indented copy -- how these forks actually arrive -- still matches
    # rather than hiding behind the extra spaces.
    assert detect(f"x = 1\n{repeated}y = 2\n{detached}"), "a re-indented copy is still a clone"
    # A fork usually arrives by pasting between modules, so the same block in two different files is one
    # finding, and both sites have to be named.
    across = _duplicated_comment_runs(
        [
            (REPO_ROOT / "app" / "a.py", f"x = 1\n{repeated}"),
            (REPO_ROOT / "app" / "b.py", f"y = 2\n{repeated}"),
        ]
    )
    assert len(across) == 1 and "app/a.py:2 and app/b.py:2" in across[0], across


RULES = Registry(
    (
        JS_ENDPOINTS_FILTER_IS_NOT_REINLINED,
        ROBOTS_PATTERN_COMPILE_LIVES_BEHIND_THE_PER_HOST_CACHE,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """Every rule must reject what it forbids, and spare what it resembles."""
    RULES.check_all_bite()
