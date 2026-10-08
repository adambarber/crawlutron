"""A helper that hands back a URL has to have consulted the host policy first.

Every function in this module is about one URL-returning helper: if it returns
something that will be fetched, the host policy that decides what may be
fetched has to have been applied on the way out. That is a small sentence, and it
is the one thing these detectors exist to keep true.

It is also the hardest detector in the suite to express, because "consulted the
policy" has no single syntactic form. A helper can check a parameter name, read
``self._host_policy``, call a validator directly, call a URL parser and discard
the result, branch on the policy before returning, or build the URL from parts
and validate only the finished string. Each of those is a different shape, and a
detector that handled only some of them would pass on the others -- which is how
a second, looser implementation of the same rule appeared here in the first
place.

The proof work is part of the rule, not a separate suite. Each detection shape
has a bite that must fire and a look-alike that must not, and the look-alikes are
written out rather than assumed: a helper that returns ``None`` did not return a
URL, and a helper that returns its own parameter without touching the policy is
this gate's business only when it is one that gets fetched.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    functions,
    local_nodes,
    local_subtree,
    offender_at,
    parent_map,
    resolved_call_name,
)
from tests.rules import Bites, Rule

_HOST_POLICY_VALIDATORS = frozenset(
    {
        "admit_or_none",
        "filter_js_endpoints",
        "NormalizedUrl",
        "_validated_url",
    }
)

#: Method calls that admit under the receiver's OWN embedded policy, so no
#: policy argument appears at the call site. ``NormalizedUrl.resolve`` is the
#: one such door on URL values in this tree (``Path.resolve`` hits the same
#: leaf but its result is type-invalid as a URL, so it cannot launder one).
_HOST_POLICY_SELF_VALIDATED_CALLS = frozenset({"resolve"})


_HOST_POLICY_URL_CALLS = frozenset({"urljoin", "urlunsplit", "urlparse", "urlsplit", "geturl"})


_HOST_POLICY_URL_FIELDS = frozenset({"url", "raw", "href", "location", "normalized_url", "final_url"})


_HOST_POLICY_NAMES = frozenset({"policy", "host_policy"})


#: Attribute spellings that hold a ``HostPolicy``. Separate from
#: ``_HOST_POLICY_NAMES`` because a *parameter* called ``policy`` conventionally
#: receives one of these objects when it is handed to a URL validator, while an
#: *attribute* called ``_policy`` is usually the crawl policy dict -- so the
#: attribute rule cannot be the looser one.
#:
#: ``_host_policy`` had to be here. The ``self.`` branch below matched
#: ``self.policy`` and ``self.host_policy`` only, and this codebase writes
#: ``self._host_policy`` throughout: the rule had never once fired on a method
#: that validated through the instance's own policy, because it could not see
#: one. A leading underscore does not change what the attribute holds.
_HOST_POLICY_ATTRS = frozenset({"policy", "host_policy", "_host_policy"})


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

    Both name sets were folded to lower case here rather than on one side only.
    The two predicates this replaces spelled the same question twice -- one
    lower-cased the leaf name and one did not -- which is correct only for as
    long as every entry in both sets happens to be written in lower case. A
    mixed-case entry added to the set that skipped the fold would have stopped
    matching, and nothing would have said so: the detector would find one fewer
    way to consult the policy, find nothing wrong, and report clean.
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

    The attribute form (``url.final_url``) and the subscript form
    (``url["final_url"]``) are one question with two spellings, and so is the
    ``url.get("final_url")`` call the caller below handles. Answering them in
    three places is how the dict form came to check a different field set than
    the attribute form -- a URL field the walk missed is an unvalidated URL
    reaching a host-policy call, and the guard would have reported the tree clean.
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
        # Reading a URL field (``.raw`` included) off a name the walk already
        # knows is policy-validated is the sanctioned text projection: the
        # value was admitted before the text was taken, so the text is clean.
        receiver_validated = isinstance(node.value, ast.Name) and node.value.id in validated_names
        return True, receiver_validated
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and _reads_url_field(node.value, node.slice.value, url_names)
    ):
        return True, False
    if isinstance(node, ast.Call):
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
            child_states = [
                _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
                for child in (*node.args, *(keyword.value for keyword in node.keywords))
            ]
            return any(is_url for is_url, _ in child_states), any(
                is_validated for _, is_validated in child_states
            )
        return False, False
    if isinstance(node, (ast.IfExp, ast.BoolOp)):
        # A ternary is only as safe as its unsafe branch, and so is ``a or b``:
        # URL-shaped if any branch is, validated only when EVERY URL-shaped
        # branch is. A non-URL branch (``... else None``) is neutral, not a
        # bypass -- it cannot carry a URL at all.
        branches = (node.body, node.orelse) if isinstance(node, ast.IfExp) else tuple(node.values)
        states = [
            _host_policy_expression_state(tree, branch, url_names, validated_names, policy_names)
            for branch in branches
        ]
        is_url = any(url for url, _ in states)
        return is_url, all(valid or not url for url, valid in states)
    if isinstance(node, ast.Dict):
        dict_states = [
            _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value in _HOST_POLICY_URL_FIELDS
        ]
        return any(is_url for is_url, _ in dict_states), any(is_validated for _, is_validated in dict_states)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        states = [
            _host_policy_expression_state(tree, element, url_names, validated_names, policy_names)
            for element in node.elts
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        state = _host_policy_expression_state(tree, node.elt, url_names, validated_names, policy_names)
        generator_url = any(
            _url_parameter_name(generator.iter.id)
            for generator in node.generators
            if isinstance(generator.iter, ast.Name)
        )
        condition_validated = any(
            _host_policy_call_is_validated(tree, child, policy_names)
            for generator in node.generators
            for condition in generator.ifs
            for child in local_nodes(condition)
            if isinstance(child, ast.Call)
        )
        return state[0] or generator_url, state[1] or condition_validated
    if isinstance(node, ast.DictComp):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in (node.key, node.value)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.BinOp, ast.BoolOp, ast.JoinedStr, ast.IfExp, ast.Compare)):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in ast.iter_child_nodes(node)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, ast.NamedExpr):
        return _host_policy_expression_state(tree, node.value, url_names, validated_names, policy_names)
    return False, False


def _url_return_shapes(function: ast.AST, tree: ast.AST) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    url_names = {argument.arg for argument in arguments if _url_parameter_name(argument.arg)}
    # A parameter annotated ``NormalizedUrl`` arrives admitted: the value
    # object's constructor is the sole validation chokepoint, so the policy
    # was applied at the caller before this function was entered. The walk
    # gives such a parameter the same "validated" verdict it gives a value
    # admitted inside the body, so ``.raw``/``.host``/... reads off it count
    # as the sanctioned text projection instead of an unvalidated URL.
    admitted = {
        argument.arg
        for argument in arguments
        if isinstance(argument.annotation, ast.Name) and argument.annotation.id == "NormalizedUrl"
    }
    url_names |= admitted
    validated_names: set[str] = set(admitted)
    policy_names = _host_policy_scope_names(tree, function)
    nodes = local_subtree(function)
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
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            elif isinstance(statement, ast.NamedExpr):
                targets = [statement.target]
                value = statement.value
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
            "from app.models.normalized_url import admit_or_none\n"
            "def resolve(url, *, policy: HostPolicy):\n"
            "    validator = admit_or_none\n"
            "    return validator(url)\n",
            "returns URLs without required HostPolicy",
        ),
        Bites(
            "from urllib.parse import urlunsplit\ndef rebuild(parts):\n    return urlunsplit(parts)\n",
            "returns URLs without required HostPolicy",
        ),
    ),
    clean=lambda: (
        "from app.models.normalized_url import admit_or_none\n"
        "def validated(url, *, policy: HostPolicy):\n"
        "    return admit_or_none(url, policy=policy)\n",
        "from app.models.normalized_url import admit_or_none\n"
        "class JobStore:\n"
        "    def __init__(self, host_policy):\n"
        "        self._host_policy = host_policy\n"
        "    def canonical_root(self, root_url):\n"
        "        return str(admit_or_none(root_url, policy=self._host_policy))\n",
        "from urllib.parse import urljoin\n"
        "from app.models.normalized_url import admit_or_none\n"
        "def resolve(root, path, *, policy: HostPolicy):\n"
        "    return admit_or_none(urljoin(root, path), policy=policy)\n",
        "class Client:\n"
        "    policy: HostPolicy\n"
        "    def emit(self, url):\n"
        "        return admit_or_none(url, policy=self.policy)\n",
        "from app.models.normalized_url import admit_or_none as validate\n"
        "def resolve(url, *, policy: HostPolicy):\n"
        "    return validate(url, policy=policy)\n",
        "from urllib.parse import urlunsplit\n"
        "from app.models.normalized_url import admit_or_none\n"
        "def rebuild(parts, *, policy: HostPolicy):\n"
        "    return admit_or_none(urlunsplit(parts), policy=policy)\n",
        # A parameter annotated NormalizedUrl is admitted at its own
        # construction boundary -- the sole chokepoint -- so projecting .raw
        # from it inside a helper is the sanctioned text projection, not a
        # policy bypass. The policy travels with the type, not a parameter.
        "def connect(url: NormalizedUrl, headers: dict[str, str]):\n"
        "    return url.raw, dict(headers)\n",
    ),
)


def test_required_host_policy_detector_bites():
    URL_RETURNING_HELPERS_REQUIRE_HOST_POLICY.check_bites()


def test_required_host_policy_detector_covers_new_url_helper_shapes():
    source = ast.parse(
        "def passthrough_url(url: str) -> str:\n"
        "    return url\n"
        "def filter_endpoints(endpoints: list[dict[str, str]]) -> list[dict[str, str]]:\n"
        "    return [item for item in endpoints if item.get('url')]\n"
    )
    offenders = _required_host_policy_offenders(
        source,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 2, offenders


def test_host_policy_detector_accepts_fixed_extraction_boundaries():
    path = REPO_ROOT / "app" / "extraction" / "content.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not [
        offender
        for offender in _required_host_policy_offenders(tree, path)
        if any(name in offender for name in ("frames_from", "forms_from"))
    ]


def test_url_returning_helpers_require_host_policy():
    URL_RETURNING_HELPERS_REQUIRE_HOST_POLICY.scan()
