"""Evaluation engine for the XPath subset: turns parsed steps into DOM nodes.

Imports nothing from the parser; kept separate so the parse module stays small.
"""

from __future__ import annotations

from crawlutron_extract.html_dom import DOMNode


def _apply_predicate(candidates: list[DOMNode], pred: tuple) -> list[DOMNode]:
    """Filter a candidate node set by one predicate, applied in order.

    ``[n]`` and ``[last()]`` use positions within ``candidates`` (the XPath
    context set); attribute and function predicates are position-independent.
    """
    kind = pred[0]
    if kind == "PRED_ATTR_EXISTS":
        return [node for node in candidates if node.get_attribute(pred[1]) is not None]
    if kind == "PRED_ATTR_EQ":
        return [
            node for node in candidates if node.get_attribute(pred[1]) == _eval_predicate_value(pred[2], node)
        ]
    if kind == "PRED_FUNC":
        return [node for node in candidates if _eval_function_call(pred[1], node) is True]
    if kind == "PRED_POS":
        n = pred[1]
        return [c for i, c in enumerate(candidates) if i + 1 == n]
    if kind == "PRED_LAST":
        return [candidates[-1]] if candidates else []
    return candidates


def _eval_predicate_value(value: tuple, node: DOMNode) -> str | None:
    kind = value[0]
    if kind == "STRING_LITERAL":
        return value[1]
    if kind == "FUNC_CALL":
        result = _eval_function_call(value, node)
        return result if isinstance(result, str) else None
    return None


def _eval_function_call(func: tuple, node: DOMNode):
    """Evaluate a function call against ``node``; returns str or bool."""
    name = func[1]
    args = func[2]
    evaluated = [_eval_function_arg(a, node) for a in args]
    if name in ("contains", "starts-with"):
        if len(evaluated) != 2 or not all(isinstance(a, str) for a in evaluated):
            return False
        left, right = evaluated
        return right in left if name == "contains" else left.startswith(right)
    if name == "normalize-space":
        if len(evaluated) == 1 and isinstance(evaluated[0], str):
            return " ".join(evaluated[0].split())
        if len(evaluated) == 0:
            return " ".join(node.get_text().split())
    if name == "concat":
        return "".join(a for a in evaluated if isinstance(a, str))
    return None


def _eval_function_arg(arg: tuple, node: DOMNode):
    kind = arg[0]
    if kind == "STRING_LITERAL":
        return arg[1]
    if kind == "ATTR_REF":
        return node.get_attribute(arg[1])
    if kind == "FUNC_CALL":
        return _eval_function_call(arg, node)
    return None


def _eval_step(nodes: list[DOMNode], descendant: bool, step: tuple) -> list[DOMNode]:
    kind = step[0]
    if kind == "TEXT_NODE":
        return list(nodes)
    if kind == "ATTR_NODE":
        return [node for node in nodes if node.get_attribute(step[1]) is not None]
    # NODE_TEST
    name_test = step[1]
    predicates = step[2]
    # Gather every candidate matching the node test, in document order.
    candidates: list[DOMNode] = []
    for node in nodes:
        pool = node.children if not descendant else _descendants(node)
        for cand in pool:
            if name_test != "*" and cand.tag != name_test:
                continue
            candidates.append(cand)
    # Apply predicates sequentially, each over the running result set.
    for pred in predicates:
        candidates = _apply_predicate(candidates, pred)
    return candidates


def _descendants(node: DOMNode) -> list[DOMNode]:
    out: list[DOMNode] = []
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(reversed(cur.children))
    return out


def select(root: DOMNode, expression: str) -> list[DOMNode]:
    """Evaluate an XPath expression over ``root``, returning matched DOM nodes."""
    from crawlutron_extract.xpath_select import parse_xpath

    steps = parse_xpath(expression)
    if not steps:
        return [root]
    current: list[DOMNode] = [root]
    for descendant, step in steps:
        current = _eval_step(current, descendant, step)
    return current


def select_values(root: DOMNode, expression: str) -> list[str]:
    """Evaluate an XPath expression returning string values (text() / @attr)."""
    from crawlutron_extract.xpath_select import parse_xpath

    steps = parse_xpath(expression)
    if not steps:
        return [root.get_text()]
    current: list[DOMNode] = [root]
    value_step: tuple | None = None
    for descendant, step in steps:
        current = _eval_step(current, descendant, step)
        if step[0] in ("TEXT_NODE", "ATTR_NODE"):
            value_step = step
    values: list[str] = []
    if value_step is not None and value_step[0] == "TEXT_NODE":
        for node in current:
            values.append(node.get_text())
    elif value_step is not None and value_step[0] == "ATTR_NODE":
        for node in current:
            val = node.get_attribute(value_step[1])
            if val is not None:
                values.append(val)
    return values
