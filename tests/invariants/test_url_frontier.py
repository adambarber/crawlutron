"""Frontier links, host policies and path returns stay validated."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    app_trees,
    callable_defs,
    functions,
    nodes_of,
    offender_at,
    resolved_call_name,
    returns,
    try_blocks,
)
from tests.invariants._shared import function_defs
from tests.rules import Bites, Registry, Rule

# --------------------------------------------------------------------------- Frontier raw-href sinks ()
# ---------------------------------------------------------------------------


def _url_key_reads(node: ast.AST) -> bool:
    """True if the subtree reads dict key ``"url"`` (the unvalidated raw href)."""
    for sub in nodes_of(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "get"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and sub.args[0].value == "url"
            ):
                return True
        elif isinstance(sub, ast.Subscript):
            if isinstance(sub.slice, ast.Constant) and sub.slice.value == "url":
                return True
    return False


def _frontier_sink_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``*.frontier.append(x)`` where ``x`` (or its same-function binding)
    reads the raw ``"url"`` key instead of going through frontier_link_url()."""
    offenders: list[str] = []
    for fn in function_defs(tree):
        tainted: set[str] = set()
        for assign in (a for a in nodes_of(fn) if isinstance(a, ast.Assign | ast.AnnAssign)):
            value = assign.value
            if value is None or _frontier_link_url_call(value):
                continue
            if _url_key_reads(value):
                targets = [assign.target] if isinstance(assign, ast.AnnAssign) else assign.targets
                for t in targets:
                    if isinstance(t, ast.Name):
                        tainted.add(t.id)
        for call in (c for c in nodes_of(fn) if isinstance(c, ast.Call)):
            f = call.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("append", "appendleft", "extend")
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "frontier"
            ):
                for arg in call.args:
                    if _url_key_reads(arg) or (isinstance(arg, ast.Name) and arg.id in tainted):
                        offenders.append(offender_at(path, call, f"raw href into frontier.{f.attr}"))
    return offenders


def _frontier_link_url_call(node: ast.AST) -> bool:
    """True if the subtree's root or any call node is frontier_link_url(...)."""
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "frontier_link_url"
        for sub in nodes_of(node)
    )


def _optional_host_policy_offenders(tree, path):
    """A ``HostPolicy`` parameter must never be optional: the policy is the SSRF gate,
    and a default turns a forgotten argument into a silent bypass that mypy and every
    name-list gate missed -- so the rule keys off the annotation, not the name.
    """
    for node in callable_defs(tree):
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        defaulted = set()
        for index, arg in enumerate(positional):
            if index >= len(positional) - len(args.defaults):
                defaulted.add(arg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                defaulted.add(arg.arg)
        for arg in [*positional, *args.kwonlyargs]:
            if arg.arg not in defaulted:
                continue
            annotation = arg.annotation
            if isinstance(annotation, ast.Name) and annotation.id == "HostPolicy":
                yield offender_at(
                    path,
                    arg,
                    f"{getattr(node, 'name', '<lambda>')}: parameter {arg.arg!r} is a HostPolicy and "
                    r"is optional; a caller that omits "
                    r"the SSRF policy gets the default instead of a TypeError",
                )


HOST_POLICY_IS_NEVER_OPTIONAL = Rule(
    name="host-policy-is-never-optional",
    find=_optional_host_policy_offenders,
    summary=(
        "A HostPolicy parameter must be required everywhere in app/; an optional "
        "one is an SSRF bypass that neither mypy nor any other guard catches, so "
        "a caller that omits the policy must get a TypeError rather than a default"
    ),
    bite=lambda: (
        Bites(
            "async def f(url, *, policy: HostPolicy = HostPolicy((), ())):\n    return url\n",
            "is a HostPolicy and is optional",
        ),
        # A default on a *positional* HostPolicy is caught too, not just the keyword-only form the mutations
        # used.
        Bites(
            "def g(policy: HostPolicy = HostPolicy((), ())):\n    pass\n",
            "is a HostPolicy and is optional",
        ),
    ),
    clean=lambda: (
        # The required form is left alone, and so is a required parameter the rule does not key off -- it is
        # the annotation that matters, not the name.
        "async def h(url, *, policy: HostPolicy, host_policy: HostPolicy, other: str = 'x'):\n"
        "    return url\n",
    ),
)


FRONTIER_CARRIES_ONLY_LINK_VALIDATED_URLS = Rule(
    name="frontier-carries-only-link-validated-urls",
    find=_frontier_sink_offenders,
    summary=(
        "The frontier only ever carries frontier_link_url()-validated URLs; a raw "
        "href read straight out of a payload, or a same-function binding of one, "
        "is an unvalidated URL entering the crawl queue"
    ),
    bite=lambda: Bites(
        "async def _run(job, item):\n"
        "    for link in item.get('discovered_links') or []:\n"
        "        link_url = link.get('url') if isinstance(link, dict) else str(link)\n"
        "        if link_url:\n"
        "            job.frontier.append(link_url)\n",
        "frontier.append",
    ),
    clean=lambda: (
        "async def _run(job, item):\n"
        "    for link in item.get('discovered_links') or []:\n"
        "        link_url = frontier_link_url(link)\n"
        "        if link_url:\n"
        "            job.frontier.append(link_url)\n",
    ),
)


_CONTAINMENT_CALL = "relative_to"


def _unchecked_parameter_return_offenders(tree, path) -> list[str]:
    """Report path validators that return an input parameter on some branch."""
    offenders: list[str] = []
    for func in functions(tree):
        body_calls = {
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        if _CONTAINMENT_CALL not in body_calls:
            continue
        parameters = {arg.arg for arg in (*func.args.args, *func.args.posonlyargs, *func.args.kwonlyargs)}
        for node in returns(func):
            if isinstance(node.value, ast.Name) and node.value.id in parameters:
                offenders.append(
                    f"{path.name}:{node.lineno} {func.name}() returns its own parameter "
                    f"{node.value.id!r} on a branch that bypasses the containment check; "
                    f"a path validator returns a resolved, allowlisted path or None"
                )
    return offenders


PATH_VALIDATORS_RETURN_AN_ALLOWLISTED_VALUE_OR_NONE = Rule(
    name="path-validators-return-an-allowlisted-value-or-none",
    find=_unchecked_parameter_return_offenders,
    summary=(
        "A function that allowlists by resolving and testing containment must not, "
        "on some other branch, return the caller's unvalidated input; a path "
        "validator returns a resolved, allowlisted path or None"
    ),
    bite=lambda: Bites(
        "def _validate_storage_state_path(self, storage_state: str) -> str | None:\n"
        '    if storage_state in {"default", "session"}:\n'
        "        return storage_state\n"
        "    path = Path(storage_state).resolve()\n"
        "    for allowed in allowed_dirs:\n"
        "        try:\n"
        "            path.relative_to(allowed)\n"
        "            return str(path)\n"
        "        except ValueError:\n"
        "            continue\n"
        "    return None\n",
        "returns its own parameter",
    ),
    clean=lambda: (
        # The fixed shape: only a resolved, allowlisted path ever comes back.
        "def _validate_storage_state_path(self, storage_state: str) -> str | None:\n"
        "    path = Path(storage_state).resolve()\n"
        "    for allowed in allowed_dirs:\n"
        "        try:\n"
        "            path.relative_to(allowed)\n"
        "            return str(path)\n"
        "        except ValueError:\n"
        "            continue\n"
        "    return None\n",
        # Without a containment check there is nothing to bypass, so a function that echoes its input is not
        # this gate's business.
        "def _label(name: str) -> str:\n    return name\n",
        # Returning a derived value, or None, is what the contract allows.
        "def _resolve(base: str, name: str) -> str | None:\n"
        "    path = (Path(base) / name).resolve()\n"
        "    path.relative_to(base)\n"
        "    return str(path)\n",
    ),
)


def test_resolved_call_name_handles_getattr_alias_and_static_name():
    source = ast.parse(
        "from builtins import getattr as ga\n"
        "import ipaddress\n"
        "def direct():\n"
        "    return getattr(ipaddress, 'ip_address')\n"
        "def aliased(value):\n"
        "    return ga(ipaddress, 'ip_address')(value)\n"
        "def constructed(value):\n"
        "    name = 'ip_' + 'address'\n"
        "    return getattr(ipaddress, name)(value)\n"
    )
    calls = [node for node in nodes_of(source) if isinstance(node, ast.Call)]
    attribute_calls = [call for call in calls if isinstance(call.func, ast.Name)]
    assert len(attribute_calls) == 3, (
        "the call-name resolver must see the static name, the getattr alias and the "
        f"aliased-ga spelling; found {len(attribute_calls)}: {attribute_calls}"
    )
    assert all(resolved_call_name(source, call) == "ipaddress.ip_address" for call in attribute_calls)


# --------------------------------------------------------------------------- Normalize-or-None chokepoint


def _normalize_or_none_offenders(tree, path):
    """A try/except ValueError around the constructor outside the chokepoint."""
    for node in try_blocks(tree):
        body_names = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in nodes_of(node)
            if isinstance(n, ast.Call)
        }
        if "NormalizedUrl" not in body_names:
            continue
        for handler in node.handlers:
            if not (isinstance(handler.type, ast.Name) and handler.type.id == "ValueError"):
                continue
            # The re-inlined wrapper discards the exception: its body is a bare None, pass, or a raw fallback
            # (``x`` returned unchanged).
            body = handler.body
            if len(body) != 1:
                continue
            stmt = body[0]
            is_discard = (
                (
                    isinstance(stmt, ast.Return)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is None
                )
                or isinstance(stmt, ast.Pass)
                or (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Name)
                    and stmt.value.id != stmt.targets[0].id
                )
            )
            if is_discard:
                yield offender_at(
                    path,
                    node,
                    "NormalizedUrl(...) + except ValueError "
                    "re-inlined; use safe_url.normalized_url.admit_or_none",
                )


NORMALIZE_OR_NONE_IS_NOT_REINLINED = Rule(
    name="normalize-or-none-is-not-reinlined",
    find=_normalize_or_none_offenders,
    summary=(
        "A hand-rolled try/except ValueError around the NormalizedUrl constructor "
        "re-derives the discard policy at one egress door; admit_or_none is the "
        "one wrapper every door must share"
    ),
    # The chokepoint's own definition is the sanctioned site.
    scope=lambda *, exclude: app_trees(exclude=frozenset({"normalized_url.py"})),
    bite=lambda: Bites(
        "def f(u, p):\n"
        "    try:\n"
        "        return NormalizedUrl(u, policy=p)\n"
        "    except ValueError:\n"
        "        return None\n",
        "admit_or_none",
    ),
    clean=lambda: ("def f(u, p):\n    return admit_or_none(u, policy=p)\n",),
)


RULES = Registry(
    (
        HOST_POLICY_IS_NEVER_OPTIONAL,
        FRONTIER_CARRIES_ONLY_LINK_VALIDATED_URLS,
        PATH_VALIDATORS_RETURN_AN_ALLOWLISTED_VALUE_OR_NONE,
        NORMALIZE_OR_NONE_IS_NOT_REINLINED,
    )
)


def test_no_rule_violations():
    """Policies are required, frontiers validated, validators allowlisted, admits chokepointed."""
    RULES.check_all_clean()
