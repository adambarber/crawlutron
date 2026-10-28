from __future__ import annotations

import ast

from tests.chokepoint_walks import app_offenders
from tests.invariants._encoding_sets import (
    _ALWAYS_DECODABLE_ENCODINGS,
    _ENCODING_TOKEN,
    _KNOWN_ENCODINGS,
)


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
