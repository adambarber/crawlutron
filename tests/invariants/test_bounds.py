"""Untrusted input is bounded before it is used.

Decompression, response bodies, header numbers, declared encodings and
extracted text all arrive from a host we do not control. The cap is declared
once, passed at the call, and reachable by no other route."""

from __future__ import annotations

import ast
import pathlib
import re

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    TESTS_DIR,
    app_offenders,
    app_trees,
    assert_detector,
    assert_fires_once,
    assert_stays_clean,
    assignments,
    attribute_calls,
    attribute_reads,
    call_nodes,
    from_imports,
    functions,
    nodes_of,
    offender_at,
    plain_imports,
    resolved_call_name,
    resolved_expr_name,
    writes,
)
from tests.invariants._shared import assert_trees_clean

# ---------------------------------------------------------------------------
# A bounded HTML input has exactly one cap
# ---------------------------------------------------------------------------

# Two modules independently declared a 4 MB cap on the HTML they would scan --
# `_MAX_HTML_NOISE_INPUT_BYTES` in classify.py and
# `_MAX_LLM_FORMAT_HTML_INPUT_BYTES` in llm_format.py -- with the same value and
# the same rationale ("a hostile origin will hand you gigabytes"), reached by two
# names that no reader could see were the same policy. Raising one cap silently
# left the other, so the guarantee a reviewer believed was enforced per-consumer
# was actually enforced for whichever module happened to be edited. The shape is
# the same as every other drift-prone twin here: a resource bound is a policy, and
# a policy with two declarations is a policy that will disagree with itself. The
# cap now lives once in app/bounded_io.py next to the stream caps that share its
# purpose, and this gate keeps the second copy from reappearing.


def _html_input_cap_name(target: ast.expr, value: ast.expr | None) -> str | None:
    """The name of a locally-declared HTML input byte cap, or ``None``.

    Keyed on the *intent* (an HTML byte budget) rather than the two names it used
    to have, so a third spelling is caught too. A cap that delegates elsewhere --
    anything that is not a numeric expression -- is not a second declaration, and
    that is one predicate rather than the two guards each copy of this rule used
    to carry.
    """
    if value is None or not _is_numeric_expr(value):
        return None
    name = target.id if isinstance(target, ast.Name) else ""
    head = name.upper()
    return name if "HTML" in head and head.endswith("BYTES") else None


def _local_html_input_caps(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a module that declares its own HTML input byte cap."""
    return [
        offender_at(path, node, name)
        for node, target, value in assignments(nodes_of(tree))
        if (name := _html_input_cap_name(target, value)) is not None
    ]


def _inline_extraction_normalizations(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an inline ``<something>.text or \"\"`` outside the extraction normalizer.

    Keyed on the *normalization itself* -- an extractor's text being coerced to
    a string -- rather than on the extractor branches that happened to spell it,
    so a fourth extractor is caught too. An ``or \"\"`` on a non-``.text``
    attribute is somebody else's business.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        if len(node.values) != 2:
            continue
        left, right = node.values
        if not (isinstance(left, ast.Attribute) and left.attr == "text"):
            continue
        if isinstance(right, ast.Constant) and right.value == "":
            offenders.append(offender_at(path, node, "extraction-text-normalization"))
    return offenders


def test_extraction_text_is_normalized_by_one_owner():
    """An extractor's text is coerced to a string in exactly one place.

    ``_extract_document``/``_extract_html``/``extract_content`` each had the
    same ``extracted.text or ""`` tail spelled at their call site. That
    coercion is what keeps an extractor's ``None`` text from reaching a result
    at all, so a branch that omitted it produced a None that only surfaced
    further downstream. ``extraction.normalized_extraction`` is the owner; it
    lives beside ``ExtractedContent`` because the coercion is a fact about
    extractor output, not about any one strategy's result builder.
    """
    offenders = app_offenders(_inline_extraction_normalizations, exclude=SANCTIONED | {"extraction.py"})
    assert not offenders, "extraction text is normalized inline instead of by its owner:\n  " + "\n  ".join(
        offenders
    )


def test_extraction_text_normalization_detector_bites():
    """The normalization sweep must fire on a hand-rolled one, and not on a
    lookalike -- otherwise it either misses the shape or flags a pattern that
    belongs to somebody else."""
    assert_detector(
        _inline_extraction_normalizations,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={'def f(e):\n    return e.text or ""\n': "extraction-text-normalization"},
        clean={'def f(d):\n    return d.label or ""\n'},
    )


def _is_numeric_expr(node: ast.expr) -> bool:
    """True for a literal/computed number, e.g. ``4 * 1024 * 1024``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return True
    return isinstance(node, ast.BinOp) and _is_numeric_expr(node.left) and _is_numeric_expr(node.right)


def test_html_input_cap_is_declared_once():
    """The bounded-HTML-input cap has one declaration, in app/bounded_io.py.

    app/bounded_io.py is exempt because it IS the chokepoint that owns the cap
    alongside the stream caps.
    """
    assert_trees_clean(
        _local_html_input_caps,
        "Import MAX_HTML_INPUT_BYTES and call bounded_io.truncate_html_input "
        "instead of declaring a second HTML input cap",
        trees=app_trees(exclude=SANCTIONED | {"bounded_io.py"}),
    )


def test_local_html_input_cap_detector_bites():
    """Every twin shape fires; delegating and unrelated names stay clean."""
    path = TESTS_DIR.parent / "app" / "_synthetic.py"
    fires = {
        # The two historical spellings, verbatim.
        "_MAX_HTML_NOISE_INPUT_BYTES = 4 * 1024 * 1024\n": "_MAX_HTML_NOISE_INPUT_BYTES",
        "_MAX_LLM_FORMAT_HTML_INPUT_BYTES = 4 * 1024 * 1024\n": "_MAX_LLM_FORMAT_HTML_INPUT_BYTES",
        # Annotated, and a plain literal.
        "HTML_INPUT_BYTES: int = 4194304\n": "HTML_INPUT_BYTES",
        # A third name for the same policy must not slip past the intent keying.
        "MAX_DOC_HTML_BYTES = 5_000_000\n": "MAX_DOC_HTML_BYTES",
    }
    assert_fires_once(_local_html_input_caps, fires, path)

    clean = (
        # The sanctioned import-and-delegate shape.
        "from app.bounded_io import MAX_HTML_INPUT_BYTES, truncate_html_input\n",
        "body = truncate_html_input(html)\n",
        # Unrelated byte budgets: the sink has its own, and it is not HTML input.
        "MAX_WEBHOOK_RESPONSE_BYTES = 10_485_760\n",
        "MAX_CACHE_ENTRIES = 100_000\n",
        "HTML_FIELDS = ('title', 'description')\n",
    )
    assert_stays_clean(_local_html_input_caps, clean, path)


# ---------------------------------------------------------------------------
# Decompression bomb guard (jscpd #5 family / H2 closure)
# ---------------------------------------------------------------------------

_DECOMPRESS_FUNCS = frozenset({"decompress_body", "_decompress_body"})


_DECOMPRESS_SANCTIONED = frozenset({"decompression.py"})


def _decompress_cap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a decompress_body call that omits the mandatory output cap.

    A small gzip/deflate/brotli stream can expand to gigabytes; every
    production caller must bound the output. The cap is now a required
    keyword at the chokepoint, so this AST sweep is belt-and-suspenders
    against a caller reaching for an ``int`` default or ``**kwargs``.
    """
    offenders: list[str] = []
    for node in call_nodes(tree):
        if resolved_expr_name(tree, node.func).rsplit(".", 1)[-1] == "getattr":
            continue
        fname = resolved_call_name(tree, node).rsplit(".", 1)[-1]
        if fname not in _DECOMPRESS_FUNCS:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "max_output_bytes" not in kwargs:
            offenders.append(offender_at(path, node, f"{fname}(...) without max_output_bytes"))
    return offenders


def test_all_decompress_calls_pass_a_cap():
    """Every production decompression is size-bounded (H2 closure)."""
    assert_trees_clean(
        _decompress_cap_offenders,
        "decompress_body needs max_output_bytes=",
        trees=app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED),
    )


def test_decompress_cap_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "_synthetic.py"
    bad = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc)\n")
    offenders = _decompress_cap_offenders(bad, path)
    assert len(offenders) == 1, offenders

    alias_shapes = {
        "import as": (
            "import app.crawl.decompression as compression\n"
            "def f(raw, enc):\n"
            "    return compression.decompress_body(raw, enc)\n"
        ),
        "from import as": (
            "from app.crawl.decompression import decompress_body as decode\n"
            "def f(raw, enc):\n"
            "    return decode(raw, enc)\n"
        ),
        "assignment alias": (
            "from app.crawl.decompression import decompress_body\n"
            "decode = decompress_body\n"
            "def f(raw, enc):\n"
            "    return decode(raw, enc)\n"
        ),
        "getattr": (
            "import app.crawl.decompression as compression\n"
            "def f(raw, enc):\n"
            "    return getattr(compression, 'decompress_body')(raw, enc)\n"
        ),
    }
    for label, source in alias_shapes.items():
        offenders = _decompress_cap_offenders(ast.parse(source), path)
        assert len(offenders) == 1, (label, offenders)

    good = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc, max_output_bytes=1024)\n")
    assert not _decompress_cap_offenders(good, path)


# ---------------------------------------------------------------------------
# Decompression reaches only through the capped chokepoint (legacy #3)
# ---------------------------------------------------------------------------

# The codecs app/crawl/decompression.py imports to implement decompress_body.
_DECOMPRESS_CODECS = frozenset({"gzip", "zlib", "brotli"})


_DECOMPRESS_CODEC_METHODS = frozenset({"decompress", "decompressobj", "GzipFile", "Decompressor"})


def _decompress_bypass_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag raw stdlib decompression (import or call) outside the chokepoint.

    All body decoding must route through ``decompression.decompress_body``, which
    makes ``max_output_bytes`` mandatory and returns input unchanged on decoder
    error (so a forged ``Content-Encoding`` cannot expand or corrupt unbounded).
    A module that imports or calls ``gzip``/``zlib``/``brotli`` directly can
    re-introduce the header-trusting, uncapped decompress the legacy cleanup
    removed — the ``_decompress_cap`` sweep only guards the chokepoint's own
    callers, so this closes the bypass around it.
    """
    return (
        [
            offender_at(path, node, f"imports decompression codec '{alias.name}' (bypasses decompress_body)")
            for node in plain_imports(tree)
            for alias in node.names
            if alias.name.split(".")[0] in _DECOMPRESS_CODECS
        ]
        + [
            offender_at(
                path, node, f"imports decompression codec from '{node.module}' (bypasses decompress_body)"
            )
            for node in from_imports(tree)
            if (node.module or "").split(".")[0] in _DECOMPRESS_CODECS
        ]
        + [
            offender_at(
                path,
                call,
                f"calls {call.func.value.id}.{call.func.attr}() directly (bypasses decompress_body)",
            )
            for call, func in attribute_calls(tree)
            if isinstance(func.value, ast.Name)
            and func.value.id in _DECOMPRESS_CODECS
            and func.attr in _DECOMPRESS_CODEC_METHODS
        ]
    )


def test_decompression_reaches_only_through_the_chokepoint():
    """No app module touches gzip/zlib/brotli except the decompression chokepoint."""
    assert_trees_clean(
        _decompress_bypass_offenders,
        "decompress only via decompression.decompress_body()",
        trees=app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED),
    )


def test_decompress_bypass_detector_bites():
    """A header-driven raw decompress fires on both import and call."""
    # Two offenders from one source: the import AND the call are each a bypass, so
    # a source that imports the codec but routes through decompress_body has to
    # report 1, not 2 -- the count is what proves the detector is not just
    # keying on the import.
    assert_detector(
        _decompress_bypass_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        trips={
            "import gzip\n"
            "def f(raw, hdr):\n"
            "    if 'gzip' in hdr.get('content-encoding', ''):\n"
            "        return gzip.decompress(raw)\n": 2,
        },
        clean=(
            "from app.crawl.decompression import decompress_body\n"
            "def f(raw, hdr):\n"
            "    return decompress_body(raw, hdr.get('content-encoding', ''), max_output_bytes=1024)\n",
        ),
    )


# ---------------------------------------------------------------------------
# A discovery response is read through _Discovery, never off the tuple (jscpd)
#
# The jscpd pair at fetch_helpers.py was the two discovery-fetch signatures, but
# the rule underneath them was never owned: what a discovery response *means*.
# The load-bearing part of that is ``headers: dict | None``, where None is the
# transport-failure marker. ``_Discovery``'s accessors are total over it, so a
# consumer cannot reach a ``.get`` on a missing map. Take the header map back and
# the guard has to be re-derived by hand -- which is the shape that was actually
# duplicated, three hand-written projections of one positional 3-tuple each
# repeating the ``is None`` + status test. Nothing but this guard stops the
# fourth projection from landing.
# ---------------------------------------------------------------------------

# The fetch that returns a _Discovery. The other two discovery helpers project
# that response to caller shapes, so naming this one is what makes the rule
# about the RESULT rather than about a call site.
_DISCOVERY_FETCH = "_fetch_decode_discovery"


# The chokepoint module that legitimately owns what it bans.
_DISCOVERY_CHOKEPOINT = "app/crawl/fetch_helpers.py"


def _is_discovery_fetch(value: ast.AST | None) -> bool:
    # The result is normally reached through ``await``, so the assignment's value
    # is the Await node and the call is inside it. Unwrapping one layer is what
    # keeps the detector from being clean on a tree that does the banned thing.
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    leaf = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return leaf == _DISCOVERY_FETCH


def _discovery_response_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag reading a discovery response off the tuple instead of through _Discovery.

    Both spellings are offenders, because both restore the reach the accessors
    close. Destructuring is the one this replaced:
    ``text, status, resp_headers = await _fetch_decode_discovery(...)`` followed
    by ``resp_headers.get("etag")`` puts the ``dict | None`` back in the caller's
    hands with the ``is None`` test re-derived by hand. A detector that only
    understood the attribute form would call that tree clean, so the two walks
    are reported separately and both are required.
    """
    nodes = writes(tree)
    bound: set[str] = set()
    destructured: list[ast.AST] = []
    for node, target, value in assignments(nodes):
        if not _is_discovery_fetch(value):
            continue
        if isinstance(target, ast.Name):
            bound.add(target.id)
        else:
            destructured.append(node)
    return [
        offender_at(path, node, "destructures a discovery response off the tuple") for node in destructured
    ] + [
        offender_at(path, node, "reads a discovery response's header map directly")
        for node in attribute_reads(tree)
        if node.attr == "headers" and isinstance(node.value, ast.Name) and node.value.id in bound
    ]


def test_discovery_response_is_read_through_one_owner():
    """Only _Discovery's accessors touch a discovery response's header map."""
    offenders = app_offenders(_discovery_response_offenders, skip=(_DISCOVERY_CHOKEPOINT,))
    assert not offenders, "Read a discovery response via _Discovery accessors: " + ", ".join(offenders)


def test_discovery_response_detector_bites():
    """Both restoration shapes fire; going through the accessors stays clean."""
    bad = ast.parse(
        "async def f(fetcher):\n"
        "    text, status, resp_headers = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return text, status, resp_headers.get('etag')\n"
    )
    offenders = _discovery_response_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    assert "destructures a discovery response" in offenders[0], offenders
    attr_bad = ast.parse(
        "async def f(fetcher):\n"
        "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.headers\n"
    )
    offenders = _discovery_response_offenders(attr_bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    assert "header map directly" in offenders[0], offenders
    good = ast.parse(
        "async def f(fetcher):\n"
        "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.validators(), resp.not_modified(), resp.text, resp.status\n"
    )
    assert not _discovery_response_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Header ints are guarded (L2): a hostile header must never crash a fetch
# ---------------------------------------------------------------------------


def _is_int_or_float_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"int", "float"}


def _reads_headers(node: ast.AST) -> bool:
    """True for ``x.headers.get(...)`` or ``x.headers[...]`` -- a raw header read."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        source = node.func.value
        return node.func.attr == "get" and isinstance(source, ast.Attribute) and source.attr == "headers"
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Attribute) and node.value.attr == "headers"
    return False


def _catches_value_error(node: ast.Try) -> bool:
    """True if any handler catches ValueError (incl. via a tuple/Exception)."""
    for handler in node.handlers:
        exc = handler.type
        names = (
            {elt.id for elt in exc.elts if isinstance(elt, ast.Name)}
            if isinstance(exc, ast.Tuple)
            else {exc.id}
            if isinstance(exc, ast.Name)
            else set()
        )
        if names & {"ValueError", "Exception", "BaseException"}:
            return True
    return False


def _unguarded_header_int_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``int``/``float`` of a header-derived value outside a try/except ValueError.

    Parsing a ``Content-Length`` (or any header) into a number without guarding
    the conversion is the L2 class: one crafted ``Content-Length: abc`` raises
    out of the fetcher and fails the whole request. The safe shapes either go
    through a guard predicate (the arg is a parameter, not an in-scope header
    read) or wrap the ``int()`` in ``try/except ValueError`` (middleware's
    request-cap check). Tree-wide because any module can grow a header read.
    """
    offenders: list[str] = []
    for func in functions(tree):
        header_names = {
            target.id
            for node in nodes_of(func)
            if isinstance(node, ast.Assign) and _reads_headers(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        guarded = {
            id(sub)
            for node in nodes_of(func)
            if isinstance(node, ast.Try) and _catches_value_error(node)
            for sub in nodes_of(node)
            if _is_int_or_float_call(sub)
        }
        for node in nodes_of(func):
            if not _is_int_or_float_call(node) or id(node) in guarded or not node.args:
                continue
            arg = node.args[0]
            is_derived = _reads_headers(arg) or (isinstance(arg, ast.Name) and arg.id in header_names)
            if is_derived:
                offenders.append(offender_at(path, node, f"{func.name}() parses a header number unguarded"))
    return offenders


def test_header_numbers_are_converted_under_guard():
    """No unguarded int()/float() on a header value anywhere in app/ (L2)."""
    assert_trees_clean(
        _unguarded_header_int_offenders,
        "parse header numbers through a guard or try/except ValueError",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_header_int_detector_bites():
    bad = ast.parse(
        "async def fetcher(resp):\n    content_length = resp.headers.get('content-length')\n"
        "    return int(content_length) > 100\n"
    )
    offenders = _unguarded_header_int_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    inline_bad = ast.parse(
        "async def fetcher(resp):\n    return int(resp.headers.get('content-length', 0)) > 100\n"
    )
    assert _unguarded_header_int_offenders(inline_bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    guarded = ast.parse(
        "def guard(value):\n    try:\n        return int(value)\n    except ValueError:\n        return 0\n"
    )
    assert not _unguarded_header_int_offenders(guarded, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # A sanitizer takes a parameter, not a live header read -- never flagged.
    param_based = ast.parse(
        "def _declared_length_exceeds(header_value, limit):\n    return int(header_value) > limit\n"
    )
    assert not _unguarded_header_int_offenders(param_based, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Advertise only the encodings this process can decode
# ---------------------------------------------------------------------------

# Request headers named `br` unconditionally while the brotli decoder is an
# OPTIONAL dependency -- app/crawl/decompression supports two incompatible
# brotli APIs precisely because either may be the one installed. So on a host
# without it, every brotli-capable server was told to return a body the process
# would then refuse, and the crawl failed on a request we chose to make that
# way. Advertising an encoding is a promise about what this process can decode,
# so it is checked against the decoder set rather than trusted per call site.
_ALWAYS_DECODABLE_ENCODINGS = {"gzip", "deflate", "identity"}


_ENCODING_TOKEN = re.compile(r"^[a-z0-9\-_+.]+$")


# A comma-joined list is only an encoding list if it MENTIONS a real encoding.
# Without this anchor the check reads English: api_models.py field descriptions
# like "dom_text, etc., http_html" are comma-joined, lowercase and token-shaped,
# and a gate that cries wolf on those gets ignored on the day it matters.
_KNOWN_ENCODINGS = {
    "gzip",
    "x-gzip",
    "deflate",
    "x-deflate",
    "br",
    "zstd",
    "compress",
    "x-compress",
    "identity",
    "*",
}


def _decodable_encoding_tokens() -> set[str]:
    """Every encoding this process can decode, which is now a fixed set.

    The conditional ``br`` this replaced came from a brotli availability probe,
    and brotli is a declared dependency -- the decoder set cannot vary by
    deployment, so asking the environment was asking a question with one answer.
    """
    return set(_ALWAYS_DECODABLE_ENCODINGS) | {"br"}


def _undecodable_encoding_offenders(tree, path, *, decodable: set[str] | None = None) -> list[str]:
    """Report encoding-list literals naming something outside the decoder set.

    ``decodable`` stays injectable so the detector's own logic can be exercised
    against a set that excludes a token the live tree does decode -- which is
    how a regression in the CHECK is caught, independently of what this
    particular process happens to have installed.
    """
    if decodable is None:
        decodable = _decodable_encoding_tokens()
    # Keyed by (line, token): ast.walk yields a dict's value Constant both under
    # its key and as a node in its own right, so without this one bad header
    # reports twice and the count stops meaning anything.
    seen: dict[tuple[int, str], str] = {}

    def check(node, value: str) -> None:
        tokens = {token.strip().lower() for token in value.split(",") if token.strip()}
        if len(tokens) < 2 or not all(_ENCODING_TOKEN.match(token) for token in tokens):
            return
        if not tokens & _KNOWN_ENCODINGS:
            return
        for token in sorted(tokens - decodable):
            seen[(node.lineno, token)] = (
                f"{path.name}:{node.lineno} advertises {token!r}, which app/crawl/decompression "
                f"cannot decode here (decodable: {', '.join(sorted(decodable))})"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.lower() == "accept-encoding"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    check(value, value.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A standalone encoding list, however it reaches the header.
            check(node, node.value)
    return [seen[key] for key in sorted(seen)]


def test_no_request_header_advertises_an_undecodable_encoding():
    """The header's claim is checked against the decoder set rather than trusted.

    Every decoder is a declared dependency, so the set is fixed and this is a
    statement about the tree rather than about the machine running it: a header
    naming a coding outside ``_DECODABLE_ENCODINGS`` (zstd, most likely, which
    would be passed through whole and served as if it were the body) is a defect
    on every deployment.
    """
    offenders = app_offenders(_undecodable_encoding_offenders)
    assert not offenders, "; ".join(offenders)


def test_undecodable_encoding_detector_bites():
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    # A decodable set that omits brotli, to exercise the check itself. It is a
    # stand-in for the check's input, not a deployment this project supports.
    without_brotli = {"gzip", "deflate", "identity"}

    # The shape that shipped: br named unconditionally, as a plain literal.
    bad = ast.parse('HEADERS = {"Accept-Encoding": "gzip, deflate, br"}\n')
    assert len(_undecodable_encoding_offenders(bad, synthetic, decodable=without_brotli)) == 1

    # Reached indirectly, as a variable rather than inside the dict literal.
    indirect = ast.parse('ENCODING = "gzip, deflate, br"\nHEADERS = {"Accept-Encoding": ENCODING}\n')
    assert len(_undecodable_encoding_offenders(indirect, synthetic, decodable=without_brotli)) == 1

    # The same literal is clean once the decoder is in the set, which is what
    # makes the verdict about the set and not about the spelling.
    assert not _undecodable_encoding_offenders(bad, synthetic, decodable={*without_brotli, "br"})

    # A genuinely undecodable token is caught even with brotli present.
    zstd = ast.parse('HEADERS = {"Accept-Encoding": "gzip, zstd"}\n')
    assert len(_undecodable_encoding_offenders(zstd, synthetic, decodable={*without_brotli, "br"})) == 1

    # A header value the detector cannot read as a literal is not a finding: the
    # check is about advertised encodings, and a computed value advertises none
    # that this gate can name.
    computed = ast.parse(
        'ENCODING = "gzip, deflate"\n'
        "if available():\n"
        '    ENCODING += ", br"\n'
        'HEADERS = {"Accept-Encoding": ENCODING}\n'
    )
    assert not _undecodable_encoding_offenders(computed, synthetic, decodable=without_brotli)

    # Not an encoding list at all: another comma-joined header is not this gate's
    # business, and neither is English prose that happens to be comma-joined.
    other = ast.parse(
        'HEADERS = {"Accept": "text/html,application/xml;q=0.9"}\n'
        'OTHER = "dom_text, etc., http_html"\n'
        "SINGLE = 'gzip'\n"
    )
    assert not _undecodable_encoding_offenders(other, synthetic, decodable=without_brotli)
