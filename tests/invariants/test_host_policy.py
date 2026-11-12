"""A helper that hands back a URL must have consulted the host policy first.

"Consulted the policy" has no single syntactic form, and a detector covering
only some shapes passes on the others; the proof work is part of the rule.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    functions,
    local_nodes,
    local_subtree,
    offender_at,
    parent_map,
    parsed,
    resolved_call_name,
)
from tests.rules import Bites, Rule, Trips

_HOST_POLICY_VALIDATORS = frozenset(
    {
        "admit_or_none",
        "filter_js_endpoints",
        "normalizedurl",
        "_validated_url",
    }
)

#: Method calls that admit under the receiver's OWN embedded policy, so no policy argument appears at the call
#: site.
_HOST_POLICY_SELF_VALIDATED_CALLS = frozenset({"resolve"})


_HOST_POLICY_URL_CALLS = frozenset({"urljoin", "urlunsplit", "urlparse", "urlsplit", "geturl"})


_HOST_POLICY_URL_FIELDS = frozenset({"url", "raw", "href", "location", "normalized_url", "final_url"})


_HOST_POLICY_NAMES = frozenset({"policy", "host_policy"})


#: Attribute spellings that hold a ``HostPolicy``.
_HOST_POLICY_ATTRS = frozenset({"policy", "host_policy", "_host_policy", "_policy"})


def _url_parameter_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(("_of", "_getter", "_accessor")):
        return False
    return (
        "url" in lowered
        or lowered in {"href", "location", "origin", "endpoint", "endpoints"}
        or lowered.endswith("_href")
    )


def _is_policy_call(tree: ast.AST, node: ast.Call, policy_names: frozenset[str]) -> bool:
    """Whether this call is one of the sanctioned ways to reach a URL policy.

    Both name sets are folded here, not on one side: a mixed-case entry that
    skipped the fold would silently stop matching and report clean.
    """
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1].lower() in policy_names


def _host_policy_value(node: ast.AST | None, policy_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in policy_names
    if isinstance(node, ast.Attribute) and node.attr in _HOST_POLICY_ATTRS:
        return isinstance(node.value, ast.Name) and node.value.id == "self"
    return False


def _host_policy_scope_names(tree: ast.AST, function: ast.AST) -> set[str]:
    parents = parent_map(tree)
    names: set[str] = set()
    current: ast.AST | None = function
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (*current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs)
            names.update(argument.arg for argument in arguments if argument.arg in _HOST_POLICY_NAMES)
        current = parents.get(id(current))
    return names


def _host_policy_empty_guards(function: ast.AST) -> set[int]:
    guarded: set[int] = set()
    for node in local_nodes(function):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        empty = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
        ) or (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and any(isinstance(op, (ast.Is, ast.Eq)) for op in test.ops)
            and any(
                isinstance(comparator, ast.Constant) and comparator.value in {None, ""}
                for comparator in test.comparators
            )
        )
        if empty:
            guarded.update(id(child) for child in node.body if isinstance(child, ast.Return))
    return guarded


def _host_policy_call_is_validated(tree: ast.AST, node: ast.Call, policy_names: set[str]) -> bool:
    if resolved_call_name(tree, node).rsplit(".", 1)[-1].lower() in _HOST_POLICY_SELF_VALIDATED_CALLS:
        return True
    if not _is_policy_call(tree, node, _HOST_POLICY_VALIDATORS):
        return False
    return any(
        _host_policy_value(value, policy_names)
        for value in (*node.args, *(keyword.value for keyword in node.keywords))
    )


def _reads_url_field(receiver, field, url_names: set[str]) -> bool:
    """True when ``field`` is read off a name the walk already knows is a URL.

    Attribute and subscript are one question with two spellings; answering them
    separately is how the dict form checked a different field set.
    """
    return isinstance(receiver, ast.Name) and receiver.id in url_names and field in _HOST_POLICY_URL_FIELDS


def _host_policy_expression_state(
    tree: ast.AST,
    node: ast.AST | None,
    url_names: set[str],
    validated_names: set[str],
    policy_names: set[str],
) -> tuple[bool, bool]:
    if node is None:
        return False, False
    if isinstance(node, ast.Name):
        return node.id in url_names, node.id in validated_names
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.lower().startswith(("http://", "https://")), False
    if isinstance(node, ast.Attribute) and _reads_url_field(node.value, node.attr, url_names):
        receiver_validated = isinstance(node.value, ast.Name) and node.value.id in validated_names
        return True, receiver_validated
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and _reads_url_field(node.value, node.slice.value, url_names)
    ):
        return True, False
    if isinstance(node, ast.Call):
        return _call_site_state(tree, node, (url_names, validated_names, policy_names))
    if isinstance(node, (ast.IfExp, ast.BoolOp)):
        return _branch_state(node, tree, (url_names, validated_names, policy_names))
    if isinstance(node, ast.Dict):
        dict_states = [
            _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value in _HOST_POLICY_URL_FIELDS
        ]
        return any(is_url for is_url, _ in dict_states), any(is_validated for _, is_validated in dict_states)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return _merge(_children_states(node.elts, tree, url_names, validated_names, policy_names))
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _comprehension_state(node, tree, (url_names, validated_names, policy_names))
    if isinstance(node, ast.DictComp):
        return _merge(
            _children_states((node.key, node.value), tree, url_names, validated_names, policy_names)
        )
    if isinstance(node, (ast.BinOp, ast.BoolOp, ast.JoinedStr, ast.IfExp, ast.Compare)):
        return _merge(
            _children_states(
                tuple(ast.iter_child_nodes(node)), tree, url_names, validated_names, policy_names
            )
        )
    if isinstance(node, ast.NamedExpr):
        return _host_policy_expression_state(tree, node.value, url_names, validated_names, policy_names)
    return False, False


"""Shared shape for the sub-evaluators below: the walk context plus the verdict."""

WalkContext = tuple[set[str], set[str], set[str]]
"""(url_names, validated_names, policy_names) -- the walk's three name sets."""


def _children_states(
    children,
    tree: ast.AST,
    url_names: set[str],
    validated_names: set[str],
    policy_names: set[str],
) -> list[tuple[bool, bool]]:
    """Each child's (is_url, is_validated), in order."""
    return [
        _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
        for child in children
    ]


def _merge(states: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    """URL-shaped if any child is; validated if any child is."""
    return any(is_url for is_url, _ in states), any(is_validated for _, is_validated in states)


def _call_site_state(tree: ast.AST, node: ast.Call, context: WalkContext) -> tuple[bool, bool]:
    """A call is URL-shaped when it validates, builds from policy, or reads a
    URL field; str/format/join inherit their arguments' states."""
    url_names, validated_names, policy_names = context
    if _host_policy_call_is_validated(tree, node, policy_names):
        return True, True
    if _is_policy_call(tree, node, _HOST_POLICY_VALIDATORS):
        return True, False
    if _is_policy_call(tree, node, _HOST_POLICY_URL_CALLS):
        return True, False
    if resolved_call_name(tree, node).rsplit(".", 1)[-1] == "get" and node.args:
        key = node.args[0]
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        return (isinstance(key, ast.Constant) and _reads_url_field(receiver, key.value, url_names)), False
    if resolved_call_name(tree, node).rsplit(".", 1)[-1] in {"str", "format", "join"}:
        return _merge(
            _children_states(
                (*node.args, *(keyword.value for keyword in node.keywords)),
                tree,
                url_names,
                validated_names,
                policy_names,
            )
        )
    return False, False


def _branch_state(node: ast.IfExp | ast.BoolOp, tree: ast.AST, context: WalkContext) -> tuple[bool, bool]:
    """A ternary is only as safe as its unsafe branch, and so is ``a or b``: URL-shaped if any branch is,
    validated only when EVERY URL-shaped branch is. A non-URL branch (``...
    """
    url_names, validated_names, policy_names = context
    branches = (node.body, node.orelse) if isinstance(node, ast.IfExp) else tuple(node.values)
    states = _children_states(branches, tree, url_names, validated_names, policy_names)
    is_url = any(url for url, _ in states)
    return is_url, all(valid or not url for url, valid in states)


def _comprehension_state(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp, tree: ast.AST, context: WalkContext
) -> tuple[bool, bool]:
    """A comprehension carries its element's state, plus the generator's URL
    names and any validator call in its conditions."""
    state = _host_policy_expression_state(tree, node.elt, *context)
    generator_url = any(
        _url_parameter_name(generator.iter.id)
        for generator in node.generators
        if isinstance(generator.iter, ast.Name)
    )
    condition_validated = any(
        _host_policy_call_is_validated(tree, child, context[2])
        for generator in node.generators
        for condition in generator.ifs
        for child in local_nodes(condition)
        if isinstance(child, ast.Call)
    )
    return state[0] or generator_url, state[1] or condition_validated


def _url_return_shapes(function: ast.AST, tree: ast.AST) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    url_names = {argument.arg for argument in arguments if _url_parameter_name(argument.arg)}
    # The walk gives such a parameter the same "validated" verdict it gives a value admitted inside the body,
    # so ``.raw``/``.host``/...
    admitted = {
        argument.arg
        for argument in arguments
        if isinstance(argument.annotation, ast.Name) and argument.annotation.id == "NormalizedUrl"
    }
    url_names |= admitted
    validated_names: set[str] = set(admitted)
    policy_names = _host_policy_scope_names(tree, function)
    nodes = local_subtree(function)
    _propagate_loop_and_assign_states(tree, nodes, (url_names, validated_names, policy_names))
    return _endpoint_leaks(tree, function, nodes, arguments, (url_names, validated_names, policy_names))


def _propagate_loop_and_assign_states(
    tree: ast.AST, nodes: tuple[ast.AST, ...], context: WalkContext
) -> None:
    """The fixed-point walk: loops over a URL iterable name their targets, and
    assignments inherit their value's state, until nothing new is learned."""
    url_names, validated_names, policy_names = context
    changed = True
    while changed:
        changed = False
        for loop in (node for node in nodes if isinstance(node, ast.For)):
            if not isinstance(loop.iter, ast.Name) or loop.iter.id not in url_names:
                continue
            target_names = (
                {target.id for target in loop.target.elts if isinstance(target, ast.Name)}
                if isinstance(loop.target, (ast.Tuple, ast.List))
                else ({loop.target.id} if isinstance(loop.target, ast.Name) else set())
            )
            url_names.update(target_names)
            if any(
                _host_policy_call_is_validated(tree, child, policy_names)
                for child in local_nodes(loop)
                if isinstance(child, ast.Call)
            ):
                validated = {loop.iter.id, *target_names}
                changed |= bool(validated - validated_names)
                validated_names.update(validated)
        for statement in nodes:
            targets, value = _assign_parts(statement)
            if value is None:
                continue
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if not names:
                continue
            is_url, is_validated = _host_policy_expression_state(
                tree, value, url_names, validated_names, policy_names
            )
            if is_url:
                changed |= bool(names - url_names)
                url_names.update(names)
            if is_validated:
                changed |= bool(names - validated_names)
                validated_names.update(names)
            if _host_policy_value(value, policy_names):
                changed |= bool(names - policy_names)
                policy_names.update(names)


def _assign_parts(statement: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """The (targets, value) an assignment-shaped statement writes."""
    if isinstance(statement, ast.Assign):
        return statement.targets, statement.value
    if isinstance(statement, ast.AnnAssign):
        return [statement.target], statement.value
    if isinstance(statement, ast.NamedExpr):
        return [statement.target], statement.value
    return [], None


def _endpoint_leaks(
    tree: ast.AST,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    nodes: tuple[ast.AST, ...],
    arguments: tuple[ast.arg, ...],
    context: WalkContext,
) -> bool:
    """Whether any returned expression is an unvalidated URL or a raw endpoint
    parameter -- the two shapes the gate refuses."""
    url_names, validated_names, policy_names = context
    endpoint_parameters = {
        argument.arg for argument in arguments if argument.arg.lower() in {"endpoint", "endpoints"}
    }
    empty_guards = _host_policy_empty_guards(function)
    for statement in nodes:
        if not isinstance(statement, ast.Return) or id(statement) in empty_guards:
            continue
        is_url, is_validated = _host_policy_expression_state(
            tree, statement.value, url_names, validated_names, policy_names
        )
        if is_url and not is_validated:
            return True
        if isinstance(statement.value, ast.Name) and statement.value.id in endpoint_parameters:
            return True
    return False


def _required_host_policy_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in functions(tree):
        if _url_return_shapes(node, tree):
            found.append(offender_at(path, node, f"{node.name}() returns URLs without required HostPolicy"))
    return found


URL_RETURNING_HELPERS_REQUIRE_HOST_POLICY = Rule(
    name="url-returning-helpers-require-host-policy",
    find=_required_host_policy_offenders,
    summary=(
        "Every URL boundary must apply the host policy before forwarding a URL, "
        "so a function that returns URLs must both require a HostPolicy and route "
        "the returned URL through it; an optional, unused, or absent policy is a bypass"
    ),
    bite=lambda: (
        Bites(
            "def normalize_url(url, *, policy=None):\n    return url\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "class _unpromoted_pinned_url:\n    def parse(self, url, *, policy=None):\n        return url\n",
            "returns URLs without required HostPolicy",
        ),
        # A method with no policy at all must still be caught.
        Bites(
            "class JobStore:\n    def canonical_root(self, root_url):\n        return str(root_url)\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "from urllib.parse import urljoin\ndef resolve(root, path):\n    return urljoin(root, path)\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "def emit(url, *, policy: HostPolicy):\n    return url\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "from crawlutron.models.normalized_url import admit_or_none\n"
            "def resolve(url, *, policy: HostPolicy):\n"
            "    validator = admit_or_none\n"
            "    return validator(url)\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "from urllib.parse import urlunsplit\ndef rebuild(parts):\n    return urlunsplit(parts)\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "def hand_back(url): return NormalizedUrl(url)\n",
            "returns URLs without required HostPolicy",
        ),
        # Two helper shapes at once, each its own finding: a bare passthrough and a comprehension
        # filter -- the shape a future helper actually arrives as.
        Trips(
            "def passthrough_url(url: str) -> str:\n"
            "    return url\n"
            "def filter_endpoints(endpoints: list[dict[str, str]]) -> list[dict[str, str]]:\n"
            "    return [item for item in endpoints if item.get('url')]\n",
            count=2,
        ),
    ),
    clean=lambda: (
        "from crawlutron.models.normalized_url import admit_or_none\n"
        "def validated(url, *, policy: HostPolicy):\n"
        "    return admit_or_none(url, policy=policy)\n",
        "from crawlutron.models.normalized_url import admit_or_none\n"
        "class JobStore:\n"
        "    def __init__(self, host_policy):\n"
        "        self._host_policy = host_policy\n"
        "    def canonical_root(self, root_url):\n"
        "        return str(admit_or_none(root_url, policy=self._host_policy))\n",
        "from urllib.parse import urljoin\n"
        "from crawlutron.models.normalized_url import admit_or_none\n"
        "def resolve(root, path, *, policy: HostPolicy):\n"
        "    return admit_or_none(urljoin(root, path), policy=policy)\n",
        "class Client:\n"
        "    policy: HostPolicy\n"
        "    def emit(self, url):\n"
        "        return admit_or_none(url, policy=self.policy)\n",
        "from crawlutron.models.normalized_url import admit_or_none as validate\n"
        "def resolve(url, *, policy: HostPolicy):\n"
        "    return validate(url, policy=policy)\n",
        "from urllib.parse import urlunsplit\n"
        "from crawlutron.models.normalized_url import admit_or_none\n"
        "def rebuild(parts, *, policy: HostPolicy):\n"
        "    return admit_or_none(urlunsplit(parts), policy=policy)\n",
        # A parameter annotated NormalizedUrl is admitted at its own construction boundary -- the sole
        "def connect(url: NormalizedUrl, headers: dict[str, str]):\n    return url.raw, dict(headers)\n",
    ),
)


def test_host_policy_detector_accepts_fixed_extraction_boundaries():
    """Sanction narrowness: frames_from/forms_from stay unflagged.

    An extraction boundary takes a bound, not a policy; if the detector
    ever flags these, the sanction needs a second look.
    """
    path = APP_ROOT / "extraction" / "content.py"
    tree = parsed(path)
    assert not [
        offender
        for offender in _required_host_policy_offenders(tree, path)
        if any(name in offender for name in ("frames_from", "forms_from"))
    ]


def test_url_returning_helpers_require_host_policy():
    URL_RETURNING_HELPERS_REQUIRE_HOST_POLICY.scan()
