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
    app_offenders,
    assignments,
    attribute_calls,
    attribute_reads,
    call_nodes,
    from_imports,
    functions,
    nodes_of,
    offender_at,
    or_nodes,
    plain_imports,
    resolved_call_name,
    resolved_expr_name,
    writes,
)
from tests.rules import Bites, Registry, Rule, Trips

# That coercion is what keeps an extractor's ``None`` text from reaching a result at all, so a branch that
# omitted it produced a None that only surfaced further downstream.


def _inline_extraction_normalizations(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an inline ``<something>.text or ""`` outside the extraction normalizer.

    Keyed on the *normalization itself* -- an extractor's text being coerced to
    a string -- rather than on the extractor branches that happened to spell it,
    so a fourth extractor is caught too. An ``or ""`` on a non-``.text``
    attribute is somebody else's business.
    """
    offenders: list[str] = []
    for node in or_nodes(tree):
        left, right = node.values
        if not (isinstance(left, ast.Attribute) and left.attr == "text"):
            continue
        if isinstance(right, ast.Constant) and right.value == "":
            offenders.append(offender_at(path, node, "extraction-text-normalization"))
    return offenders


EXTRACTION_TEXT_NORMALIZATION = Rule(
    name="extraction-text-normalization",
    find=_inline_extraction_normalizations,
    summary="normalizes extraction text inline instead of through extraction.normalized_extraction",
    excludes=SANCTIONED | {"discovery.py"},  # the module that OWNS normalized_extraction
    bite=lambda: Bites('def f(e):\n    return e.text or ""\n', "extraction-text-normalization"),
    clean=lambda: ('def f(d):\n    return d.label or ""\n',),
)


# The shape is the same as every other drift-prone twin here: a resource bound is a policy, and a policy with
# two declarations is a policy that will disagree with itself.


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


def _is_numeric_expr(node: ast.expr) -> bool:
    """True for a literal/computed number, e.g. ``4 * 1024 * 1024``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return True
    return isinstance(node, ast.BinOp) and _is_numeric_expr(node.left) and _is_numeric_expr(node.right)


HTML_INPUT_CAP = Rule(
    name="local-html-input-cap",
    find=_local_html_input_caps,
    summary=(
        "declares a second HTML input byte cap; import MAX_HTML_INPUT_BYTES from "
        "app/safety/bounded_io.py and call truncate_html_input instead"
    ),
    # app/safety/bounded_io.py is exempt because it IS the chokepoint that owns the cap alongside the stream
    # caps.
    excludes=SANCTIONED | {"bounded_io.py"},
    bite=lambda: (
        # The two historical spellings, verbatim.
        Bites("_MAX_HTML_NOISE_INPUT_BYTES = 4 * 1024 * 1024\n", "_MAX_HTML_NOISE_INPUT_BYTES"),
        Bites("_MAX_LLM_FORMAT_HTML_INPUT_BYTES = 4 * 1024 * 1024\n", "_MAX_LLM_FORMAT_HTML_INPUT_BYTES"),
        # Annotated, and a plain literal.
        Bites("HTML_INPUT_BYTES: int = 4194304\n", "HTML_INPUT_BYTES"),
        # A third name for the same policy must not slip past the intent keying.
        Bites("MAX_DOC_HTML_BYTES = 5_000_000\n", "MAX_DOC_HTML_BYTES"),
    ),
    clean=lambda: (
        # The sanctioned import-and-delegate shape.
        "from app.safety.bounded_io import MAX_HTML_INPUT_BYTES, truncate_html_input\n",
        "body = truncate_html_input(html)\n",
        # Unrelated byte budgets: the sink has its own, and it is not HTML input.
        "MAX_WEBHOOK_RESPONSE_BYTES = 10_485_760\n",
        "MAX_CACHE_ENTRIES = 100_000\n",
        "HTML_FIELDS = ('title', 'description')\n",
    ),
)


# --------------------------------------------------------------------------- Decompression bomb guard
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


def _decompress_cap_proofs() -> tuple[Bites, ...]:
    """Every shape a cap-less call arrives under, each of which must be caught.

    The four alias forms are the reason this is a resolved-name sweep rather
    than a substring match: the property is about the function that is called,
    and an alias is the cheapest way to defeat a detector that reads the source
    text. The getattr form is listed because it is the one spelling a plain
    attribute read cannot resolve.
    """
    calls = "    return {call}(raw, enc)\n"
    return (
        Bites("def f(raw, enc):\n" + calls.format(call="decompress_body"), "without max_output_bytes"),
        Bites(
            "import app.transport.decompression as compression\ndef f(raw, enc):\n"
            + calls.format(call="compression.decompress_body"),
            "without max_output_bytes",
        ),
        Bites(
            "from app.transport.decompression import decompress_body as decode\ndef f(raw, enc):\n"
            + calls.format(call="decode"),
            "without max_output_bytes",
        ),
        Bites(
            "from app.transport.decompression import decompress_body\ndecode = decompress_body\n"
            "def f(raw, enc):\n" + calls.format(call="decode"),
            "without max_output_bytes",
        ),
        Bites(
            "import app.transport.decompression as compression\ndef f(raw, enc):\n"
            + calls.format(call="getattr(compression, 'decompress_body')"),
            "without max_output_bytes",
        ),
    )


DECOMPRESS_CAP = Rule(
    name="decompress-cap",
    find=_decompress_cap_offenders,
    summary="calls decompress_body without max_output_bytes",
    excludes=SANCTIONED | _DECOMPRESS_SANCTIONED,
    bite=_decompress_cap_proofs,
    clean=lambda: ("def f(raw, enc):\n    return decompress_body(raw, enc, max_output_bytes=1024)\n",),
)


# --------------------------------------------------------------------------- Decompression reaches only
# through the capped chokepoint ---------------------------------------------------------------------------
_DECOMPRESS_CODECS = frozenset({"gzip", "zlib", "brotli"})


_DECOMPRESS_CODEC_METHODS = frozenset({"decompress", "decompressobj", "GzipFile", "Decompressor"})


def _decompress_codec_alias(func: ast.Attribute) -> str | None:
    """The codec module an ``<alias>.<method>()`` call names, or None.

    Yields the alias rather than answering yes/no: a comprehension's ``if`` clause
    does not narrow its output expression, so the message below could only read
    ``func.value.id`` after the guard had already been written out a second time
    inline.
    """
    if isinstance(func.value, ast.Name) and func.value.id in _DECOMPRESS_CODECS:
        return func.value.id
    return None


def _decompress_bypass_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag raw stdlib decompression (import or call) outside the chokepoint.

    All body decoding must route through ``decompression.decompress_body``, which
    makes ``max_output_bytes`` mandatory and returns input unchanged on decoder
    error (so a forged ``Content-Encoding`` cannot expand or corrupt unbounded).
    A module that imports or calls ``gzip``/``zlib``/``brotli`` directly can
    re-introduce the header-trusting, uncapped decompress the legacy cleanup
    removed -- the ``_decompress_cap`` sweep only guards the chokepoint's own
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
                f"calls {name}.{func.attr}() directly (bypasses decompress_body)",
            )
            for call, func in attribute_calls(tree)
            for name in [_decompress_codec_alias(func)]
            if name is not None and func.attr in _DECOMPRESS_CODEC_METHODS
        ]
    )


DECOMPRESS_BYPASS = Rule(
    name="decompress-bypass",
    find=_decompress_bypass_offenders,
    summary="touches gzip/zlib/brotli directly instead of via decompression.decompress_body()",
    excludes=SANCTIONED | _DECOMPRESS_SANCTIONED,
    bite=lambda: (
        # Two offenders from one source: the import AND the call are each a bypass, so a source that imports
        Trips(
            "import gzip\n"
            "def f(raw, hdr):\n"
            "    if 'gzip' in hdr.get('content-encoding', ''):\n"
            "        return gzip.decompress(raw)\n",
            2,
        ),
    ),
    clean=lambda: (
        "from app.transport.decompression import decompress_body\n"
        "def f(raw, hdr):\n"
        "    return decompress_body(raw, hdr.get('content-encoding', ''), max_output_bytes=1024)\n",
    ),
)


# The load-bearing part of that is ``headers: dict | None``, where None is the transport-failure marker.
# ``_Discovery``'s accessors are total over it, so a consumer cannot reach a ``.get`` on a missing map.
_DISCOVERY_FETCH = "_fetch_decode_discovery"


# The chokepoint module that legitimately owns what it bans.
_DISCOVERY_CHOKEPOINT = "app/orchestration/fetch_helpers.py"


def _is_discovery_fetch(value: ast.AST | None) -> bool:
    # The result is normally reached through ``await``, so the assignment's value is the Await node and the
    # call is inside it.
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


DISCOVERY_RESPONSE = Rule(
    name="discovery-response",
    find=_discovery_response_offenders,
    summary="reads a discovery response off the tuple instead of through _Discovery accessors",
    skip=frozenset({_DISCOVERY_CHOKEPOINT}),
    bite=lambda: (
        Bites(
            "async def f(fetcher):\n"
            "    text, status, resp_headers = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
            "    return text, status, resp_headers.get('etag')\n",
            "destructures a discovery response",
        ),
        Bites(
            "async def f(fetcher):\n"
            "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
            "    return resp.headers\n",
            "header map directly",
        ),
    ),
    clean=lambda: (
        "async def f(fetcher):\n"
        "    resp = await _fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.validators(), resp.not_modified(), resp.text, resp.status\n",
    ),
)


# --------------------------------------------------------------------------- Header ints are guarded: a
# hostile header must never crash a fetch


def _is_int_or_float_call(call: ast.Call) -> bool:
    """True for a bare ``int(...)`` / ``float(...)``.

    Takes the ``ast.Call`` so the callers below can read ``call.args`` after the
    question is answered; a predicate over ``ast.AST`` answering ``bool`` is
    what made them re-narrow the walk to reach it.
    """
    return isinstance(call.func, ast.Name) and call.func.id in {"int", "float"}


def _reads_headers(node: ast.expr) -> bool:
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
    the conversion is its own class: one crafted ``Content-Length: abc`` raises
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
            sub
            for node in nodes_of(func)
            if isinstance(node, ast.Try) and _catches_value_error(node)
            for sub in call_nodes(node)
            if _is_int_or_float_call(sub)
        }
        for call in call_nodes(func):
            if not _is_int_or_float_call(call) or call in guarded or not call.args:
                continue
            arg = call.args[0]
            is_derived = _reads_headers(arg) or (isinstance(arg, ast.Name) and arg.id in header_names)
            if is_derived:
                offenders.append(offender_at(path, call, f"{func.name}() parses a header number unguarded"))
    return offenders


HEADER_INT_GUARD = Rule(
    name="header-int-guard",
    find=_unguarded_header_int_offenders,
    summary="converts a header number unguarded; parse through a guard or try/except ValueError",
    bite=lambda: (
        # Through a local bound from the header map.
        "async def fetcher(resp):\n    content_length = resp.headers.get('content-length')\n"
        "    return int(content_length) > 100\n",
        # And inline on the read itself.
        "async def fetcher(resp):\n    return int(resp.headers.get('content-length', 0)) > 100\n",
    ),
    clean=lambda: (
        # The try/except shape.
        "def guard(value):\n    try:\n        return int(value)\n    except ValueError:\n        return 0\n",
        # A sanitizer takes a parameter, not a live header read -- never flagged.
        "def _declared_length_exceeds(header_value, limit):\n    return int(header_value) > limit\n",
    ),
)


RULES = Registry(
    (
        EXTRACTION_TEXT_NORMALIZATION,
        HTML_INPUT_CAP,
        DECOMPRESS_CAP,
        DECOMPRESS_BYPASS,
        DISCOVERY_RESPONSE,
        HEADER_INT_GUARD,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """Every rule must reject what it forbids, and spare what it merely resembles.

    Declared on the rule rather than written here, so a detector cannot be added
    without a proof that it can fire -- and cannot be quietly weakened to match a
    proof that no longer fires.
    """
    RULES.check_all_bite()


# --------------------------------------------------------------------------- Advertise only the encodings
# this process can decode ---------------------------------------------------------------------------
_ALWAYS_DECODABLE_ENCODINGS = {"gzip", "deflate", "identity"}


_ENCODING_TOKEN = re.compile(r"^[a-z0-9\-_+.]+$")


# A comma-joined list is only an encoding list if it MENTIONS a real encoding.
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
    # Keyed by (line, token): ast.walk yields a dict's value Constant both under its key and as a node in its
    # own right, so without this one bad header reports twice and the count stops meaning anything.
    seen: dict[tuple[int, str], str] = {}

    def check(node, value: str) -> None:
        tokens = {token.strip().lower() for token in value.split(",") if token.strip()}
        if len(tokens) < 2 or not all(_ENCODING_TOKEN.match(token) for token in tokens):
            return
        if not tokens & _KNOWN_ENCODINGS:
            return
        for token in sorted(tokens - decodable):
            seen[(node.lineno, token)] = (
                f"{path.name}:{node.lineno} advertises {token!r}, which app/transport/decompression "
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


# This one rule keeps a hand-written gate and bite rather than joining RULES above, and the reason is the
# shape of its proof rather than its size. Every other rule's bite runs against one fixed configuration.


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
    # A decodable set that omits brotli, to exercise the check itself. It is a stand-in for the check's input,
    # not a deployment this project supports.
    without_brotli = {"gzip", "deflate", "identity"}

    # The shape that shipped: br named unconditionally, as a plain literal.
    bad = ast.parse('HEADERS = {"Accept-Encoding": "gzip, deflate, br"}\n')
    assert len(_undecodable_encoding_offenders(bad, synthetic, decodable=without_brotli)) == 1

    # Reached indirectly, as a variable rather than inside the dict literal.
    indirect = ast.parse('ENCODING = "gzip, deflate, br"\nHEADERS = {"Accept-Encoding": ENCODING}\n')
    assert len(_undecodable_encoding_offenders(indirect, synthetic, decodable=without_brotli)) == 1

    # The same literal is clean once the decoder is in the set, which is what makes the verdict about the set
    # and not about the spelling.
    assert not _undecodable_encoding_offenders(bad, synthetic, decodable={*without_brotli, "br"})

    # A genuinely undecodable token is caught even with brotli present.
    zstd = ast.parse('HEADERS = {"Accept-Encoding": "gzip, zstd"}\n')
    assert len(_undecodable_encoding_offenders(zstd, synthetic, decodable={*without_brotli, "br"})) == 1

    # A header value the detector cannot read as a literal is not a finding: the check is about advertised
    # encodings, and a computed value advertises none that this gate can name.
    computed = ast.parse(
        'ENCODING = "gzip, deflate"\n'
        "if available():\n"
        '    ENCODING += ", br"\n'
        'HEADERS = {"Accept-Encoding": ENCODING}\n'
    )
    assert not _undecodable_encoding_offenders(computed, synthetic, decodable=without_brotli)

    # Not an encoding list at all: another comma-joined header is not this gate's business, and neither is
    # English prose that happens to be comma-joined.
    other = ast.parse(
        'HEADERS = {"Accept": "text/html,application/xml;q=0.9"}\n'
        'OTHER = "dom_text, etc., http_html"\n'
        "SINGLE = 'gzip'\n"
    )
    assert not _undecodable_encoding_offenders(other, synthetic, decodable=without_brotli)
