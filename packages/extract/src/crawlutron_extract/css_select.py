"""CSS selector parser and evaluator for schema-driven extraction.

Supports type, class, id, attribute, combinator and pseudo selectors.
"""

from __future__ import annotations

from typing import cast

from crawlutron_extract._select_common import (
    SelectorError,
    _consume,
    _emit_char,
    _peek,
    _scan_ident,
    _skip_ws,
)
from crawlutron_extract.html_dom import DOMNode

# Tokenizer


def _scan_string(selector: str, i: int, n: int) -> int:
    """Scan a quoted string literal; raise on an unterminated (unmatched) quote."""
    quote = selector[i]
    j = i + 1
    while j < n and selector[j] != quote:
        j += 1
    if j >= n:
        raise SelectorError("Unterminated string literal", selector, quote)
    return j + 1


def _tokenize(selector: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(selector)
    while i < n:
        start = i
        c = selector[i]
        if c.isspace():
            _emit_char(tokens, "WS", " ")
            i += 1
        elif c == ".":
            _emit_char(tokens, "DOT", ".")
            i += 1
        elif c == "#":
            _emit_char(tokens, "HASH", "#")
            i += 1
        elif c == "[":
            _emit_char(tokens, "LBRACKET", "[")
            i += 1
        elif c == "]":
            _emit_char(tokens, "RBRACKET", "]")
            i += 1
        elif c == "(":
            _emit_char(tokens, "LPAREN", "(")
            i += 1
        elif c == ")":
            _emit_char(tokens, "RPAREN", ")")
            i += 1
        elif c == ",":
            _emit_char(tokens, "COMMA", ",")
            i += 1
        elif c == ">":
            _emit_char(tokens, "GREATER", ">")
            i += 1
        elif c == "+":
            _emit_char(tokens, "PLUS", "+")
            i += 1
        elif c in "^$*":
            if i + 1 < n and selector[i + 1] == "=":
                tokens.append(("ATTR_OP", c + "="))
                i += 2
            elif c == "*":
                tokens.append(("STAR", "*"))
                i += 1
            else:
                raise SelectorError("Untokenizable selector syntax", selector, selector[i])
        elif c == "=":
            if i + 1 < n and selector[i + 1] in "^$*":
                tokens.append(("ATTR_OP", selector[i : i + 2]))
                i += 2
            else:
                _emit_char(tokens, "ATTR_OP", "=")
                i += 1
        elif c in "'\"":
            j = _scan_string(selector, i, n)
            tokens.append(("STRING", selector[i:j]))
            i = j
        elif c == "/":
            tokens.append(("SLASH", "/"))
            i += 1
        elif c == ":":
            j = i + 1
            while j < n and (selector[j].isalnum() or selector[j] in "-_"):
                j += 1
            if j < n and selector[j] == "(":
                depth = 1
                j += 1
                while j < n and depth > 0:
                    if selector[j] == "(":
                        depth += 1
                    elif selector[j] == ")":
                        depth -= 1
                    j += 1
            tokens.append(("PSEUDO", selector[i:j]))
            i = j
        else:
            j = _scan_ident(selector, i, n, extra="-_")
            if j == i:
                raise SelectorError("Untokenizable selector syntax", selector, selector[i])
            tokens.append(("IDENT", selector[i:j]))
            i = j
        # Structural guard: every iteration must consume at least one character,
        # or the tokenizer spins forever on the character at the cursor.
        if i == start:
            raise SelectorError("Tokenizer stalled on token", selector, selector[start])
    return tokens


# Parser


def _parse_simple_selector(tokens: list[tuple[str, str]], pos: int) -> tuple[list[tuple], int]:
    """Parse a simple selector (type + optional class/id/attr/pseudo parts)."""
    parts: list[tuple] = []

    tok = _peek(tokens, pos)
    if tok and tok[0] == "IDENT":
        name_tok, pos = _consume(tokens, pos, "IDENT")
        parts.append(("TYPE", name_tok[1]))
    elif tok and tok[0] == "STAR":
        _consume(tokens, pos, "STAR")
        pos += 1
        parts.append(("UNIVERSAL",))

    while True:
        tok = _peek(tokens, pos)
        if tok is None:
            break
        if tok[0] == "DOT":
            _consume(tokens, pos, "DOT")
            pos += 1
            name_tok, pos = _consume(tokens, pos, "IDENT")
            parts.append(("CLASS", name_tok[1]))
        elif tok[0] == "HASH":
            _consume(tokens, pos, "HASH")
            pos += 1
            name_tok, pos = _consume(tokens, pos, "IDENT")
            parts.append(("ID", name_tok[1]))
        elif tok[0] == "LBRACKET":
            _consume(tokens, pos, "LBRACKET")
            pos += 1
            attr_name_tok, pos = _consume(tokens, pos, "IDENT")
            attr_name = attr_name_tok[1]
            op_tok = _peek(tokens, pos)
            if op_tok and op_tok[0] == "ATTR_OP":
                _consume(tokens, pos, "ATTR_OP")
                pos += 1
                op = op_tok[1]
                value_tok, pos = _consume(tokens, pos)
                value = value_tok[1].strip('"').strip("'")
                _consume(tokens, pos, "RBRACKET")
                pos += 1
                parts.append(("ATTR", attr_name, op, value))
            else:
                _consume(tokens, pos, "RBRACKET")
                pos += 1
                parts.append(("ATTR_EXISTS", attr_name))
        elif tok[0] == "PSEUDO":
            pseudo_tok, pos = _consume(tokens, pos, "PSEUDO")
            pseudo = pseudo_tok[1]
            if pseudo == ":first-child":
                parts.append(("PSEUDO_FIRST_CHILD",))
            elif pseudo == ":last-child":
                parts.append(("PSEUDO_LAST_CHILD",))
            elif pseudo.startswith(":nth-child("):
                inner = pseudo[len(":nth-child(") : -1]
                n = int(inner.strip())
                parts.append(("PSEUDO_NTH_CHILD", n))
            elif pseudo.startswith(":not("):
                inner = pseudo[len(":not(") : -1]
                inner_tokens = _tokenize(inner)
                inner_parts, inner_pos = _parse_simple_selector(inner_tokens, 0)
                if inner_pos < len(inner_tokens):
                    raise SelectorError(f"Invalid :not() selector: {inner}")
                parts.append(("PSEUDO_NOT", inner_parts))
            else:
                raise SelectorError(f"Unknown pseudo-class: {pseudo}")
        else:
            break

    return parts, pos


def _parse_selector(
    tokens: list[tuple[str, str]], pos: int
) -> tuple[list[tuple[list[tuple[str, ...]], str | None]], int]:
    """Parse a single selector chain (simple selectors separated by combinators)."""
    chain: list[tuple[list[tuple[str, ...]], str | None]] = []
    while True:
        pos = _skip_ws(tokens, pos)
        simple, pos = _parse_simple_selector(tokens, pos)
        if not simple:
            break
        pos = _skip_ws(tokens, pos)
        combinator: str | None = None
        tok = _peek(tokens, pos)
        if tok and tok[0] == "GREATER":
            _consume(tokens, pos, "GREATER")
            pos += 1
            combinator = "child"
        elif tok and tok[0] == "PLUS":
            _consume(tokens, pos, "PLUS")
            pos += 1
            combinator = "adjacent"
        chain.append((simple, combinator))
        if combinator is None:
            break
    return chain, pos


def parse_selector_list(selector: str) -> list[list[tuple[list[tuple[str, ...]], str | None]]]:
    """Parse a comma-separated selector list into selector chains.

    Each chain is a list of (simple_selector, combinator) pairs.
    The last pair in each chain has combinator None.
    """
    tokens = _tokenize(selector)
    pos = 0
    selectors: list[list[tuple[list[tuple[str, ...]], str | None]]] = []
    while pos < len(tokens):
        chain, pos = _parse_selector(tokens, pos)
        selectors.append(chain)
        tok = _peek(tokens, pos)
        if tok and tok[0] == "COMMA":
            _consume(tokens, pos, "COMMA")
            pos += 1
        else:
            break
    # Reject dangling combinators and any trailing junk.
    for chain in selectors:
        if chain and chain[-1][1] is not None:
            raise SelectorError("Dangling combinator", selector)
    if pos < len(tokens):
        raise SelectorError("Trailing tokens", selector, tokens[pos][1])
    return selectors


def validate_css(selector: str) -> None:
    """Validate a CSS selector; raise SelectorError if outside the supported subset."""
    parse_selector_list(selector)


# Matcher


def _matches_simple(node: DOMNode, parts: list[tuple[str, ...]]) -> bool:
    """Check if node matches all parts of a simple selector."""
    for part in parts:
        kind = part[0]
        if kind == "TYPE":
            if not _matches_type(node, part):
                return False
        elif kind == "UNIVERSAL":
            pass
        elif kind == "CLASS":
            if not _matches_class(node, part):
                return False
        elif kind == "ID":
            if not _matches_id(node, part):
                return False
        elif kind == "ATTR_EXISTS":
            if not _matches_attr_exists(node, part):
                return False
        elif kind == "ATTR":
            if not _matches_attr(node, part):
                return False
        elif kind == "PSEUDO_FIRST_CHILD":
            if not _matches_pseudo_first_child(node):
                return False
        elif kind == "PSEUDO_LAST_CHILD":
            if not _matches_pseudo_last_child(node):
                return False
        elif kind == "PSEUDO_NTH_CHILD":
            if not _matches_pseudo_nth_child(node, part):
                return False
        elif kind == "PSEUDO_NOT":
            if not _matches_pseudo_not(node, part):
                return False
    return True


def _matches_type(node: DOMNode, part: tuple[str, ...]) -> bool:
    return node.tag == part[1]


def _matches_class(node: DOMNode, part: tuple[str, ...]) -> bool:
    classes = node.get_attribute("class") or ""
    return part[1] in classes.split()


def _matches_id(node: DOMNode, part: tuple[str, ...]) -> bool:
    return node.get_attribute("id") == part[1]


def _matches_attr_exists(node: DOMNode, part: tuple[str, ...]) -> bool:
    return node.get_attribute(part[1]) is not None


def _matches_attr(node: DOMNode, part: tuple[str, ...]) -> bool:
    attr_name, op, value = part[1], part[2], part[3]
    attr_val = node.get_attribute(attr_name)
    if attr_val is None:
        return False
    if op == "=":
        return attr_val == value
    if op == "^=":
        return attr_val.startswith(value)
    if op == "$=":
        return attr_val.endswith(value)
    if op == "*=":
        return value in attr_val
    return False


def _matches_pseudo_first_child(node: DOMNode) -> bool:
    if not node.parent:
        return False
    return _element_index_before(node) == 0


def _matches_pseudo_last_child(node: DOMNode) -> bool:
    if not node.parent:
        return False
    siblings = node.parent.children
    for sib in reversed(siblings):
        if sib.tag:
            return sib is node
    return False


def _matches_pseudo_nth_child(node: DOMNode, part: tuple[str, ...]) -> bool:
    n = part[1]
    if not node.parent:
        return False
    return _element_index_before(node) + 1 == n


def _element_index_before(node: DOMNode) -> int:
    """Count element siblings before ``node`` in its parent's children."""
    parent = node.parent
    if parent is None:
        return 0
    index = 0
    for sib in parent.children:
        if sib is node:
            break
        if sib.tag:
            index += 1
    return index


def _matches_pseudo_not(node: DOMNode, part: tuple[str, ...]) -> bool:
    return not _matches_simple(node, cast(list[tuple[str, ...]], part[1]))


def matches_chain(node: DOMNode, chain: list[tuple[list[tuple[str, ...]], str | None]]) -> bool:
    """Check if node matches a selector chain (right-to-left evaluation)."""
    current = node
    for i in range(len(chain) - 1, -1, -1):
        simple, _ = chain[i]
        if not _matches_simple(current, simple):
            return False
        if i == 0:
            return True
        # Move to parent according to combinator between chain[i-1] and chain[i]
        combinator = chain[i - 1][1]
        if combinator == "descendant":
            found = False
            anc = current.parent
            while anc:
                if _matches_simple(anc, chain[i - 1][0]):
                    found = True
                    current = anc
                    break
                anc = anc.parent
            if not found:
                return False
        elif combinator == "child":
            if not current.parent or not _matches_simple(current.parent, chain[i - 1][0]):
                return False
            current = current.parent
        elif combinator == "adjacent":
            if not current.parent:
                return False
            siblings = current.parent.children
            try:
                idx = siblings.index(current)
            except ValueError:
                return False
            if idx == 0:
                return False
            prev = siblings[idx - 1]
            if not _matches_simple(prev, chain[i - 1][0]):
                return False
            current = prev
    return True


def matches_selector(node: DOMNode, selector_chain: list) -> bool:
    """Check if node matches a selector chain."""
    return matches_chain(node, selector_chain)


def find_all(root: DOMNode, selector_chain: list) -> list[DOMNode]:
    """Find all descendant-or-self nodes matching the selector chain."""
    results: list[DOMNode] = []

    def walk(node: DOMNode) -> None:
        if matches_chain(node, selector_chain):
            results.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return results


def matches_any(node: DOMNode, selector_list: list) -> bool:
    """Check if node matches any selector in a comma-separated list."""
    return any(matches_chain(node, chain) for chain in selector_list)


def find_all_any(root: DOMNode, selector_list: list) -> list[DOMNode]:
    """Find all descendant-or-self nodes matching any selector in the list."""
    results: list[DOMNode] = []

    def walk(node: DOMNode) -> None:
        if matches_any(node, selector_list):
            results.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return results
