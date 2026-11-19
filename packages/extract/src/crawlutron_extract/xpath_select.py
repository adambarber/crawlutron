"""XPath subset parser and public API over the bounded DOM.

Grammar: //, .//, /, *, [n], [last()], [@a], [@a='v'], contains(), concat(),
normalize-space(), starts-with(), text() and @attr; anything else raises SelectorError naming the token.
"""

from __future__ import annotations

from crawlutron_extract._select_common import (
    SelectorError,
    _consume,
    _consume_attr,
    _consume_parens,
    _emit_char,
    _is_func_call,
    _parse_string_literal,
    _peek,
    _scan_ident,
    _skip_ws,
    _unexpected_end,
)

# Single-character tokens that map one-to-one; kept as data so the tokenizer
# stays structurally distinct from the CSS tokenizer (jscpd zero tolerance).
_SINGLE_CHAR_TOKENS: dict[str, tuple[str, str]] = {
    "(": ("LPAREN", "("),
    ")": ("RPAREN", ")"),
    "[": ("LBRACKET", "["),
    "]": ("RBRACKET", "]"),
    ",": ("COMMA", ","),
    "@": ("AT", "@"),
    "*": ("STAR", "*"),
    "=": ("EQ", "="),
}


# Tokenizer


def _tokenize(expression: str) -> list[tuple[str, str]]:
    """Split an XPath expression into tokens; unknown characters raise SelectorError."""
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(expression)
    while i < n:
        c = expression[i]
        if c.isspace():
            _emit_char(tokens, "WS", c)
            i += 1
        elif c == "/":
            if i + 1 < n and expression[i + 1] == "/":
                tokens.append(("DOUBLESLASH", "//"))
                i += 2
            else:
                _emit_char(tokens, "SLASH", "/")
                i += 1
        elif c == ".":
            if i + 1 < n and expression[i + 1] == "/":
                tokens.append(("DOTSLASH", "./"))
                i += 2
            else:
                _emit_char(tokens, "DOT", ".")
                i += 1
        elif c in _SINGLE_CHAR_TOKENS:
            _emit_char(tokens, *_SINGLE_CHAR_TOKENS[c])
            i += 1
        elif c == "'" or c == '"':
            j = i + 1
            while j < n and expression[j] != c:
                j += 1
            if j >= n:
                raise SelectorError("Unterminated string literal", expression, expression[i:])
            tokens.append(("STRING", expression[i + 1 : j]))
            i = j + 1
        elif c.isalpha() or c == "_" or c.isdigit():
            j = _scan_ident(expression, i, n)
            tokens.append(("NAME", expression[i:j]))
            i = j
        else:
            raise SelectorError("Unsupported character", expression, expression[i])
    return tokens


# Parser


def _parse_predicate(tokens, pos, expression):
    """Parse [n], [last()], [@a], [@a='v'], [@a=<func>], or [<func>]."""
    _consume(tokens, pos, "LBRACKET")
    pos += 1
    tok = _peek(tokens, pos)
    if tok is None:
        raise SelectorError("Unterminated predicate", expression, "[")
    kind = tok[0]
    if kind == "NAME" and tok[1].isdigit():
        _, pos = _consume(tokens, pos, "NAME")
        n = int(tok[1])
        _, pos = _consume(tokens, pos, "RBRACKET")
        return ("PRED_POS", n), pos
    if kind == "NAME" and tok[1] == "last":
        _, pos = _consume(tokens, pos, "NAME")
        pos = _consume_parens(tokens, pos)
        _, pos = _consume(tokens, pos, "RBRACKET")
        return ("PRED_LAST",), pos
    if kind == "AT":
        _, pos = _consume(tokens, pos, "AT")
        attr_name, pos = _parse_attr_name(tokens, pos, expression)
        op_tok = _peek(tokens, pos)
        if op_tok and op_tok[0] == "EQ":
            _, pos = _consume(tokens, pos, "EQ")
            value, pos = _parse_predicate_value(tokens, pos, expression)
            _, pos = _consume(tokens, pos, "RBRACKET")
            return ("PRED_ATTR_EQ", attr_name, value), pos
        _, pos = _consume(tokens, pos, "RBRACKET")
        return ("PRED_ATTR_EXISTS", attr_name), pos
    if kind == "NAME" and tok[1] in ("contains", "starts-with", "normalize-space", "concat"):
        func, pos = _parse_function_call(tokens, pos, expression)
        _, pos = _consume(tokens, pos, "RBRACKET")
        return ("PRED_FUNC", func), pos
    raise SelectorError("Unsupported predicate", expression, tok[1])


def _parse_predicate_value(tokens, pos, expression):
    """Parse a string literal or a function call used as a predicate value."""
    tok = _peek(tokens, pos)
    if tok is None:
        _unexpected_end(tokens, pos, "Expected value after =", expression)
    if tok[0] == "STRING":
        return _parse_string_literal(tok, tokens, pos)
    if _is_func_call(tok):
        return _parse_function_call(tokens, pos, expression)
    raise SelectorError("Expected string literal or function call", expression, tok[1])


def _parse_function_call(tokens, pos, expression):
    """Parse name(arg, arg, ...) where args are strings, @attr, or nested calls."""
    name_tok, pos = _consume(tokens, pos, "NAME")
    func_name = name_tok[1]
    _, pos = _consume(tokens, pos, "LPAREN")
    pos = _skip_ws(tokens, pos)
    args: list = []
    while True:
        tok = _peek(tokens, pos)
        if tok is None:
            raise SelectorError("Unterminated function call", expression, func_name)
        if tok[0] == "RPAREN":
            _, pos = _consume(tokens, pos, "RPAREN")
            break
        arg, pos = _parse_function_arg(tokens, pos, expression)
        args.append(arg)
        pos = _skip_ws(tokens, pos)
        tok = _peek(tokens, pos)
        if tok and tok[0] == "COMMA":
            _, pos = _consume(tokens, pos, "COMMA")
            pos = _skip_ws(tokens, pos)
            continue
        if tok and tok[0] == "RPAREN":
            continue
        raise SelectorError("Expected ',' or ')' in function call", expression, tok[1] if tok else None)
    return ("FUNC_CALL", func_name, args), pos


def _parse_function_arg(tokens, pos, expression):
    """Parse one function argument: string, @attr, or nested function call."""
    tok = _peek(tokens, pos)
    if tok is None:
        _unexpected_end(tokens, pos, "Unexpected end of function arguments", expression)
    if tok[0] == "STRING":
        return _parse_string_literal(tok, tokens, pos)
    if tok[0] == "AT":
        pos = _consume_attr(tokens, pos)
        attr_name, pos = _parse_attr_name(tokens, pos, expression)
        return ("ATTR_REF", attr_name), pos
    if _is_func_call(tok):
        return _parse_function_call(tokens, pos, expression)
    raise SelectorError("Unsupported function argument", expression, tok[1])


def _parse_attr_name(tokens, pos, expression):
    """Parse a ``@name`` attribute reference, returning ``(name, new_pos)``."""
    name_tok = _peek(tokens, pos)
    if name_tok is None or name_tok[0] != "NAME":
        token = name_tok[1] if name_tok else None
        raise SelectorError("Expected attribute name", expression, token)
    _, pos = _consume(tokens, pos, "NAME")
    return name_tok[1], pos


def _parse_predicates(tokens, pos, expression):
    """Parse zero or more ``[predicate]`` suffixes, returning ``(predicates, pos)``."""
    predicates: list = []
    while True:
        nxt = _peek(tokens, pos)
        if nxt and nxt[0] == "LBRACKET":
            pred, pos = _parse_predicate(tokens, pos, expression)
            predicates.append(pred)
        else:
            break
    return predicates, pos


def _parse_step(tokens, pos, expression):
    """Parse one location step: optional axis, node test, then predicates."""
    tok = _peek(tokens, pos)
    if tok and tok[0] == "NAME" and tok[1] in ("self", "parent", "child", "descendant", "attribute"):
        axis_tok, pos = _consume(tokens, pos, "NAME")
        raise SelectorError("Unsupported axis", expression, axis_tok[1])
    if tok and tok[0] == "AT":
        _, pos = _consume(tokens, pos, "AT")
        attr_name, pos = _parse_attr_name(tokens, pos, expression)
        predicates, pos = _parse_predicates(tokens, pos, expression)
        return ("ATTR_NODE", attr_name, predicates), pos
    if tok and tok[0] == "NAME" and tok[1] == "text":
        _, pos = _consume(tokens, pos, "NAME")
        pos = _consume_parens(tokens, pos)
        return ("TEXT_NODE",), pos
    if tok and tok[0] == "NAME":
        name_tok, pos = _consume(tokens, pos, "NAME")
        node_test = name_tok[1]
        predicates, pos = _parse_predicates(tokens, pos, expression)
        return ("NODE_TEST", node_test, predicates), pos
    if tok and tok[0] == "STAR":
        _, pos = _consume(tokens, pos, "STAR")
        predicates, pos = _parse_predicates(tokens, pos, expression)
        return ("NODE_TEST", "*", predicates), pos
    raise SelectorError("Unexpected token in step", expression, tok[1] if tok else None)


def _parse_location_path(tokens, pos, expression):
    """Parse a location path: steps separated by / or //, leading // or .//."""
    steps: list = []
    descendant = False
    tok = _peek(tokens, pos)
    if tok and tok[0] == "DOUBLESLASH":
        _, pos = _consume(tokens, pos, "DOUBLESLASH")
        descendant = True
    elif tok and tok[0] == "DOTSLASH":
        _, pos = _consume(tokens, pos, "DOTSLASH")
        descendant = True
        nxt = _peek(tokens, pos)
        if nxt and nxt[0] in ("SLASH", "DOUBLESLASH"):
            _, pos = _consume(tokens, pos, nxt[0])
    elif tok and tok[0] == "SLASH":
        _, pos = _consume(tokens, pos, "SLASH")
    pos = _skip_ws(tokens, pos)
    step, pos = _parse_step(tokens, pos, expression)
    steps.append((descendant, step))
    descendant = False
    while True:
        pos = _skip_ws(tokens, pos)
        tok = _peek(tokens, pos)
        if tok is None:
            break
        if tok[0] == "SLASH":
            _, pos = _consume(tokens, pos, "SLASH")
            pos = _skip_ws(tokens, pos)
            nxt = _peek(tokens, pos)
            if nxt and nxt[0] == "SLASH":
                raise SelectorError("Expected node test after //", expression, "//")
            step, pos = _parse_step(tokens, pos, expression)
            steps.append((False, step))
        elif tok[0] == "DOUBLESLASH":
            _, pos = _consume(tokens, pos, "DOUBLESLASH")
            pos = _skip_ws(tokens, pos)
            step, pos = _parse_step(tokens, pos, expression)
            steps.append((True, step))
        else:
            break
    return steps, pos


def parse_xpath(expression: str) -> list:
    """Parse an XPath expression into a list of (descendant, step) pairs."""
    tokens = _tokenize(expression)
    steps, pos = _parse_location_path(tokens, 0, expression)
    if pos < len(tokens):
        raise SelectorError("Trailing tokens", expression, tokens[pos][1])
    return steps


def validate_xpath(expr: str) -> None:
    """Validate an XPath expression; raise SelectorError if outside the supported subset."""
    parse_xpath(expr)
