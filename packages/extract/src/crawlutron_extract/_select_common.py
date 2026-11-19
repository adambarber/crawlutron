"""Shared tokenizer helpers and the shared ``SelectorError`` for the CSS and
XPath selector parsers, kept in one module to avoid jscpd clones.
"""

from __future__ import annotations

from typing import Never


class SelectorError(Exception):
    """Raised when a selector uses syntax outside the supported subset."""

    def __init__(self, message: str, selector: str = "", token: str | None = None):
        sel = f" (selector: {selector!r})" if selector else ""
        tail = f" (token: {token!r})" if token is not None else ""
        super().__init__(f"{message}{sel}{tail}")
        self.selector = selector
        self.expression = selector
        self.token = token


def _peek(tokens: list[tuple[str, str]], pos: int) -> tuple[str, str] | None:
    if 0 <= pos < len(tokens):
        return tokens[pos]
    return None


def _consume(
    tokens: list[tuple[str, str]], pos: int, expected: str | None = None
) -> tuple[tuple[str, str], int]:
    if pos >= len(tokens):
        raise SelectorError("Unexpected end of expression")
    tok = tokens[pos]
    if expected and tok[0] != expected:
        raise SelectorError(f"Expected {expected}, got {tok[0]}", token=tok[1])
    return tok, pos + 1


def _skip_ws(tokens: list[tuple[str, str]], pos: int) -> int:
    tok = _peek(tokens, pos)
    while tok and tok[0] == "WS":
        _, pos = _consume(tokens, pos, "WS")
        tok = _peek(tokens, pos)
    return pos


def _emit_char(tokens: list[tuple[str, str]], kind: str, char: str) -> None:
    tokens.append((kind, char))


def _scan_ident(text: str, i: int, n: int, extra: str = "-_.") -> int:
    j = i
    while j < n and (text[j].isalnum() or text[j] in extra):
        j += 1
    return j


FUNC_NAMES = frozenset({"contains", "starts-with", "normalize-space", "concat"})


def _is_func_call(tok: tuple[str, str] | None) -> bool:
    return tok is not None and tok[0] == "NAME" and tok[1] in FUNC_NAMES


def _consume_parens(tokens: list[tuple[str, str]], pos: int) -> int:
    _, pos = _consume(tokens, pos, "LPAREN")
    _, pos = _consume(tokens, pos, "RPAREN")
    return pos


def _consume_attr(tokens: list[tuple[str, str]], pos: int) -> int:
    _, pos = _consume(tokens, pos, "AT")
    return pos


def _parse_string_literal(
    tok: tuple[str, str], tokens: list[tuple[str, str]], pos: int
) -> tuple[tuple[str, ...], int]:
    _, pos = _consume(tokens, pos, "STRING")
    return ("STRING_LITERAL", tok[1]), pos


def _unexpected_end(tokens: list[tuple[str, str]], pos: int, message: str, expression: str) -> Never:
    raise SelectorError(message, expression, None)
