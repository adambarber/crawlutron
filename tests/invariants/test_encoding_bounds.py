from __future__ import annotations

import ast

from tests.chokepoint_walks import REPO_ROOT, app_offenders
from tests.invariants.test_bounds import _ALWAYS_DECODABLE_ENCODINGS, _ENCODING_TOKEN, _KNOWN_ENCODINGS


def _decodable_encoding_tokens() -> set[str]:
    """Every encoding this process can decode, which is a fixed set.

    The conditional ``br`` this replaced asked the environment a question
    with one answer: brotli is a declared dependency."""
    return set(_ALWAYS_DECODABLE_ENCODINGS) | {"br"}


def _undecodable_encoding_offenders(tree, path, *, decodable: set[str] | None = None) -> list[str]:
    """Report encoding-list literals naming something outside the decoder set.

    ``decodable`` stays injectable so the check's own logic is exercisable
    against a set excluding a token the live tree does decode."""
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
    """The header's claim is checked against the decoder set not trusted.

    The set is fixed by declared dependencies, so this states a property of
    the tree: an undecodable coding would be passed through whole."""
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
