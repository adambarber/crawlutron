"""Untrusted input is bounded before it is used: decompression, bodies, header
numbers, encodings and extracted text all arrive from an uncontrolled host, so
the cap is declared once, passed at the call, reachable by no other route.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    SANCTIONED,
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
    """Flag an inline ``<something>.text or ""`` outside the normalizer.

    Keyed on the normalization itself, not the extractor branches that
    happened to spell it, so a fourth extractor is caught too."""
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
    excludes=SANCTIONED | {"discovery.py"},  # owner: discovery.py owns normalized_extraction
    bite=lambda: Bites('def f(e):\n    return e.text or ""\n', "extraction-text-normalization"),
    clean=lambda: ('def f(d):\n    return d.label or ""\n',),
)


# The shape is the same as every other drift-prone twin here: a resource bound is a policy, and a policy with
# two declarations is a policy that will disagree with itself.


def _html_input_cap_name(target: ast.expr, value: ast.expr | None) -> str | None:
    """The name of a locally-declared HTML input byte cap, or ``None``.

    Keyed on intent (an HTML byte budget), so a third spelling is caught too;
    a cap that delegates elsewhere is not a second declaration."""
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
    # owner: bounded_io.py is the cap itself
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


_DECOMPRESS_SANCTIONED = frozenset({"decompression.py"})  # owner: the codec chokepoint itself


def _decompress_cap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a decompress_body call that omits the mandatory output cap.

    A small stream can expand to gigabytes; this sweep catches int-default
    or ``**kwargs`` reach-arounds past the required keyword."""
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

    The four alias forms are why this is a resolved-name sweep rather than a
    substring match: an alias is the cheapest way to defeat a text detector."""
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

    Yields the alias rather than answering yes/no, so the offender message can
    read ``func.value.id`` without a second narrowed write."""
    if isinstance(func.value, ast.Name) and func.value.id in _DECOMPRESS_CODECS:
        return func.value.id
    return None


def _decompress_bypass_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag raw stdlib decompression (import or call) outside the chokepoint.

    All body decoding must route through decompress_body; this closes the
    bypass the cap sweep cannot see."""
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
_DISCOVERY_FETCH = "fetch_decode_discovery"


# The chokepoint module that legitimately owns what it bans.
_DISCOVERY_CHOKEPOINT = "app/transport/fetch_helpers.py"


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
    """Flag reading a discovery response off the tuple instead of _Discovery.

    Both attribute and destructured walks are reported and both are required."""
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
    skip=frozenset({_DISCOVERY_CHOKEPOINT}),  # owner: builds the Discovery tuple it guards
    bite=lambda: (
        Bites(
            "async def f(fetcher):\n"
            "    text, status, resp_headers = await fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
            "    return text, status, resp_headers.get('etag')\n",
            "destructures a discovery response",
        ),
        Bites(
            "async def f(fetcher):\n"
            "    resp = await fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
            "    return resp.headers\n",
            "header map directly",
        ),
    ),
    clean=lambda: (
        "async def f(fetcher):\n"
        "    resp = await fetch_decode_discovery(fetcher, 'u', {}, 5)\n"
        "    return resp.validators(), resp.not_modified(), resp.text, resp.status\n",
    ),
)


# --------------------------------------------------------------------------- Header ints are guarded: a
# hostile header must never crash a fetch


def _is_int_or_float_call(call: ast.Call) -> bool:
    """True for a bare ``int(...)`` / ``float(...)``.

    Takes the ast.Call so callers can read ``call.args`` after the question is
    answered, instead of re-narrowing the walk to reach it."""
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
    """Flag ``int``/``float`` of a header-derived value outside try/except ValueError.

    One crafted ``Content-Length: abc`` raises out of the fetcher and fails the
    whole request; the safe shapes guard or wrap the conversion."""
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
        "def declared_length_exceeds(header_value, limit):\n    return int(header_value) > limit\n",
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
