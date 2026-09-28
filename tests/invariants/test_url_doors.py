"""Every URL reaches a sink through a named door.

Each check here answers the same question -- "is this value a URL that a
door validated, or is it a string someone composed inline?" -- for one
class of doors: the final URL, the frontier key, the policy argument, the
redirect hop, the path validator's return."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    app_offenders,
    app_trees,
    assert_detector,
    callable_defs,
    f_strings,
    functions,
    local_nodes,
    nodes_of,
    offender,
    offender_at,
    resolved_call_name,
    returns,
    try_blocks,
)
from tests.invariants._shared import (
    _SCOPE_COMPILER_NAMES,
    _SCOPE_PATTERN_LENGTH_BOUND,
    assert_property_detector_bites,
    assert_trees_clean,
)
from tests.property_registry import Property

# --- SafeUrl chokepoint (final review) ------------------------------------

# A hand-rolled redirect hop is the "forgot one of the five steps" class SafeUrl
# exists to kill. The dangerous *shape* -- in a SINGLE function -- is: test a
# 3xx status, build the next URL with a raw urljoin, and never go through the
# chokepoint (redirect_hop) or the sanctioned pinned helper (_redirect_target).
# Link/robots/sitemap urljoin normalization never co-occurs with a 3xx test in
# the same function, so it is not a false positive -- and this runs over the
# whole tree, not a hand-picked file list.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


_REDIRECT_HOP_SANCTIONED = {"redirect_hop", "_redirect_target"}


def _functions_with(tree: ast.AST) -> list[ast.AST]:
    return [n for n in nodes_of(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]


def _uses_redirect_status(node: ast.AST) -> bool:
    """True if the function tests a concrete redirect status constant."""
    return any(isinstance(sub, ast.Constant) and sub.value in _REDIRECT_STATUSES for sub in nodes_of(node))


def _hand_rolled_redirect_hops(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for func in functions(tree):
        has_urljoin = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "urljoin"
            for n in nodes_of(func)
        )
        if not has_urljoin or not _uses_redirect_status(func):
            continue
        uses_chokepoint = any(
            isinstance(n, ast.Attribute)
            and n.attr in _REDIRECT_HOP_SANCTIONED
            or isinstance(n, ast.Name)
            and n.id in _REDIRECT_HOP_SANCTIONED
            for n in nodes_of(func)
        )
        if not uses_chokepoint:
            offenders.append(offender_at(path, func, f"{func.name}()"))
    return offenders


def test_no_hand_rolled_redirect_hops():
    """Redirect hops anywhere go through SafeUrl.redirect_hop (or the pinned
    fetch helper), never a raw urljoin combined with a 3xx status test."""
    assert_trees_clean(
        _hand_rolled_redirect_hops,
        "Use SafeUrl.redirect_hop for redirect hops",
        trees=app_trees(exclude=SANCTIONED | {"safe_url.py"}),
    )


_CANONICALIZER = frozenset({"normalize_public_http_url"})


def _raw_batch_dedup_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``dict.fromkeys`` batch dedup that keys on raw values.

    The url-batch dedup at ``_payload_urls`` must collapse duplicates by
    canonical page identity (``normalize_public_http_url``) -- not the raw
    string -- or a request carrying both ``.../x`` and ``.../x/?utm_source=tw``
    crawls one page twice. Every ordered-dedup via ``dict.fromkeys`` must sit
    inside a function that also routes through the canonicalizer, so a new
    raw-key dedup fails the suite the moment it lands.
    """
    offenders: list[str] = []
    for func in functions(tree):
        uses_fromkeys = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "fromkeys"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "dict"
            for n in nodes_of(func)
        )
        if not uses_fromkeys:
            continue
        canonicalizes = any(
            isinstance(n, ast.Name)
            and n.id in _CANONICALIZER
            or isinstance(n, ast.Attribute)
            and n.attr in _CANONICALIZER
            for n in nodes_of(func)
        )
        if not canonicalizes:
            offenders.append(
                offender_at(
                    path, func, f"{func.name}() dedups a batch with dict.fromkeys, not the canonicalizer"
                )
            )
    return offenders


def test_batch_dedup_keys_on_canonical_page_identity():
    """URL-batch dedup must collapse by ``normalize_public_http_url``, never raw strings."""
    assert_trees_clean(
        _raw_batch_dedup_offenders,
        "dedup url batches via normalize_public_http_url, not raw dict.fromkeys",
        trees=app_trees(),
    )


def test_batch_dedup_detector_bites():
    raw = ast.parse("def _payload_urls(payload):\n    return list(dict.fromkeys(payload['urls']))\n")
    assert _raw_batch_dedup_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py"), (
        "detector missed raw dict.fromkeys dedup"
    )
    good = ast.parse(
        "def _payload_urls(payload):\n"
        "    return [u for u in payload['urls'] if normalize_public_http_url(u) not in seen]\n"
    )
    assert not _raw_batch_dedup_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


def _bare_body_reads(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an unbounded body acquisition on a response-like object.

    Both ``response.read()`` (no size arg) and ``response.content`` pull the
    whole body into RAM. Capped reads go through app.bounded_io.read_capped_async;
    curl_cffi's ``.body`` attribute and non-response reads (files, sockets)
    are not HTTP body acquisitions and stay exempt. Tree-wide: any module may

    grow a fetch path, so this is keyed on the response-object convention
    rather than a directory.
    """
    offenders: list[str] = []
    for node in nodes_of(tree):
        recv = (
            node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read":
            if not node.args and not node.keywords and _is_response_like(recv):
                offenders.append(offender_at(path, node, ".read()"))
        elif isinstance(node, ast.Attribute) and node.attr == "content" and _is_response_like(node.value):
            offenders.append(offender_at(path, node, ".content"))
    return offenders


def _is_response_like(node: ast.expr | None) -> bool:
    """True for a plain-name receiver that reads as an HTTP response."""
    if not isinstance(node, ast.Name):
        return False
    head = node.id.lower()
    return head.startswith("response") or head.startswith("resp") or head in {"r"}


def test_no_bare_body_reads():
    """Every HTTP body read anywhere must be capped (N2 class).

    Scope note: app/bounded_io.py IS the chokepoint and streams the capped body
    itself -- exempt by receiver, not path, so it survives a tree-wide sweep.
    Widening kept precise on the response-object convention to avoid flagging
    file/socket reads.
    """
    assert_trees_clean(
        _bare_body_reads,
        "Use app.bounded_io.read_capped_async",
        trees=app_trees(exclude=SANCTIONED | {"bounded_io.py"}),
    )


def _scope_matcher_definitions() -> list[str]:
    """File labels of every ``def scope_allows``/``_scope_allows`` under app/.

    M10: the bounded scope check was verbatim-copied into engine.py and
    recursive.py; a one-sided fix would have left the other on the stale
    contract. Deduped into app/crawl/scope_match.py -- a second definition
    anywhere means the copy crept back.
    """
    defs: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in functions(tree):
            if node.name in {"scope_allows", "_scope_allows"}:
                defs.append(offender(path, node.lineno))
    return defs


def test_scope_matcher_has_exactly_one_definition():
    """The fail-closed scope matcher must live in one module only.

    Behavior parity across the dedupe is covered by test_scope_regex.py;
    this pins the structural property that no second copy exists to drift.
    """
    defs = _scope_matcher_definitions()
    assert len(defs) == 1, f"scope matcher duplicated again: {defs}"
    assert "app/crawl/scope_match.py" in defs[0], defs


def test_no_pinned_url_leaks_into_final_url():
    """A pinned fetch must never surface str(response.url) as final_url.

    _build_pinned_request_async rewrites a plain-HTTP host to its resolved IP
    so httpx reports the IP; surfacing it in final_url leaks topology and
    keys the cookie jar by IP. The fetchers must use current_url instead.
    Tree-wide: any module may grow a fetcher, so the final_url sink is
    checked everywhere (search providers use response.url legitimately but
    never as a final_url, so they are not false positives).
    """
    offenders = []
    for path, tree in app_trees():
        for node in nodes_of(tree):
            # final_url = str(response.url)  (assignment)
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id == "final_url"]
                if targets and _is_response_url(node.value):
                    offenders.append(offender_at(path, node, "final_url = str(response.url)"))
            # HttpFetchResult(final_url=str(response.url))  (keyword)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "final_url" and _is_response_url(kw.value):
                        offenders.append(offender_at(path, node, "final_url=str(response.url)"))
    assert not offenders, "pinned URL leaked into final_url: " + ", ".join(offenders)


def _is_response_url(node: ast.expr) -> bool:
    """True for str(response.url) -- a response object's url coerced to str."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "url"
        and isinstance(node.args[0].value, ast.Name)
        and node.args[0].value.id == "response"
    )


def _unpinned_safe_url_parses(tree, path):
    """Every SafeUrl.parse fetch consumer must pass an explicit ``pin=``.

    An unpinned parse is the H5 rebinding window: validation resolves one IP,
    then ``connect`` re-resolves at open time and a hostile DNS can swap the
    answer. Forcing an explicit pin at every call site makes "forgot to pin"
    impossible to merge; safe_url.py itself is exempt (redirect_hop passes a
    computed pin preference, and the classmethod defines the parameter).
    """
    offenders = []
    for node in nodes_of(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "parse"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "SafeUrl"
        ):
            continue
        if not any(kw.arg == "pin" for kw in node.keywords):
            offenders.append(offender_at(path, node, "SafeUrl.parse(...) must pass pin=True"))
    return offenders


def test_safe_url_parse_always_pins():
    offenders = app_offenders(_unpinned_safe_url_parses, exclude=SANCTIONED | {"safe_url.py"})
    assert not offenders, "unpinned SafeUrl.parse (DNS-rebinding window): " + "; ".join(offenders)


def test_no_inline_pinned_url_composition():
    """The pinned connect URL -- IP literal, IPv6 bracketing, port, and Host
    header composition -- is SafeUrl.connect()'s job alone.

    Three divergent copies of that composition had already drifted into the
    tree (safe_url, http fetcher, webhook builder); the copies disagreed on
    Host-port handling and all lacked IPv6 bracketing (H5-C). An f-string
    that mentions pinned_ip outside app/safe_url.py re-opens that drift.
    """
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset({"safe_url.py", "safe_regex.py"})):
        for node in f_strings(tree):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if "pinned_ip" in value.value:
                        offenders.append(offender_at(path, node, "f-string with pinned_ip"))
                if isinstance(value, ast.FormattedValue):
                    expr = value.value
                    if isinstance(expr, ast.Attribute) and expr.attr == "pinned_ip":
                        offenders.append(offender_at(path, node, "f-string interpolates .pinned_ip"))
                    if (
                        isinstance(expr, ast.Name)
                        and isinstance(expr.ctx, ast.Load)
                        and "pinned" in expr.id.lower()
                    ):
                        offenders.append(offender_at(path, node, f"f-string interpolates {expr.id}"))
    assert not offenders, "compose pinned URLs via SafeUrl.connect(), never inline:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Frontier raw-href sinks (H1 class)
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
    for fn in (n for n in nodes_of(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
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
    """A ``HostPolicy`` parameter must never be optional.

    The host policy is the SSRF gate. Every one of the 70-odd functions that
    takes one requires it, and requiring it is the point: a caller that forgot
    to pass a policy gets a ``TypeError`` at the boundary rather than a fetch
    that quietly ran against the wrong target policy.

    Defaulting one turns that into a silent bypass, and nothing else in the
    stack catches it. Verified by mutation, twice:

      * ``fetchers.impersonating_fetcher`` with
        ``policy: HostPolicy = HostPolicy((), ())`` -- ``mypy app`` reported no
        issues across 111 files, because protocol conformance is
        one-directional and an implementation may accept more than the contract
        asks for. The fetcher contract guard now covers that one specifically
        (see test_type_contracts).
      * ``pinned_transport._build_pinned_request_async`` with the same default
        -- no Protocol covers that pair, and every AST invariant in this file
        passed. This rule is what closes it.

    The rule is derived from the *type* rather than from a name list, which is
    what makes it hold for a function nobody has written yet: any
    ``HostPolicy``-annotated parameter anywhere in ``app/``, in any module, at
    any nesting depth. ``policy`` and ``host_policy`` are both in scope because
    the rule keys off the annotation, not the name.
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


def test_the_host_policy_is_never_optional():
    offenders = app_offenders(_optional_host_policy_offenders)
    assert not offenders, (
        "a HostPolicy parameter must be required everywhere in app/; an optional one is an "
        "SSRF bypass that neither mypy nor any other guard catches: " + ", ".join(offenders)
    )


def test_optional_host_policy_detector_bites():
    bad = ast.parse("async def f(url, *, policy: HostPolicy = HostPolicy((), ())):\n    return url\n")
    found = list(_optional_host_policy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(found) == 1, found
    assert "is a HostPolicy and is optional" in found[0], found

    # A default on a *positional* HostPolicy is caught too, not just the
    # keyword-only form the mutations used.
    positional = ast.parse("def g(policy: HostPolicy = HostPolicy((), ())):\n    pass\n")
    assert list(_optional_host_policy_offenders(positional, REPO_ROOT / "app" / "_synthetic.py"))

    # The required form is left alone, and so is a required parameter the rule
    # does not key off -- it is the annotation that matters, not the name.
    good = ast.parse(
        "async def h(url, *, policy: HostPolicy, host_policy: HostPolicy, other: str = 'x'):\n"
        "    return url\n"
    )
    assert not list(_optional_host_policy_offenders(good, REPO_ROOT / "app" / "_synthetic.py"))


def test_no_raw_href_frontier_sinks():
    """The frontier only ever carries frontier_link_url()-validated URLs (H1)."""
    assert_trees_clean(
        _frontier_sink_offenders,
        "Read normalized_url via frontier_link_url(), never raw url",
        trees=app_trees(),
    )


def test_raw_href_frontier_detector_bites():
    """Prove the detector fires on the historical H1 shape."""
    bad = ast.parse(
        "async def _run(job, item):\n"
        "    for link in item.get('discovered_links') or []:\n"
        "        link_url = link.get('url') if isinstance(link, dict) else str(link)\n"
        "        if link_url:\n"
        "            job.frontier.append(link_url)\n"
    )
    offenders = _frontier_sink_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1 and "frontier.append" in offenders[0], offenders


# ---------------------------------------------------------------------------
# A path validator returns a checked value, never the input it was handed
# ---------------------------------------------------------------------------

# `_validate_storage_state_path` returned the literals "default", "session" and
# "persistent" as though they named sessions. They do not: Playwright types
# `storage_state` as `Union[str, pathlib.Path] | Dict`, so a string is always
# read as a FILE PATH, and the chokepoint whose documented job is stopping a
# caller from reaching a path it should not was handing out three unvalidated
# paths relative to the process working directory. The shape of that bug is
# general: a function that allowlists by resolving and testing containment, then
# returns one of its own parameters on some other branch -- so the allowlist
# holds for most inputs and not for the ones that mattered.
#
# Detected against the containment check rather than the function name, because
# the containment check is the thing whose bypass this is. A bare
# constant-return check was tried first and was too weak: the shipped code
# returned a Name, not a literal, so it passed the gate written for it.
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


def test_path_validators_never_return_an_unchecked_parameter():
    offenders = app_offenders(_unchecked_parameter_return_offenders)
    assert not offenders, "; ".join(offenders)


def test_unchecked_parameter_return_detector_bites():
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    # The exact shape that shipped: an allowlist, plus a branch that returns the
    # caller's string before the allowlist ever sees it.
    bad = ast.parse(
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
        "    return None\n"
    )
    assert len(_unchecked_parameter_return_offenders(bad, synthetic)) == 1

    # The fixed shape: only a resolved, allowlisted path ever comes back.
    good = ast.parse(
        "def _validate_storage_state_path(self, storage_state: str) -> str | None:\n"
        "    path = Path(storage_state).resolve()\n"
        "    for allowed in allowed_dirs:\n"
        "        try:\n"
        "            path.relative_to(allowed)\n"
        "            return str(path)\n"
        "        except ValueError:\n"
        "            continue\n"
        "    return None\n"
    )
    assert not _unchecked_parameter_return_offenders(good, synthetic)

    # Without a containment check there is nothing to bypass, so a function that
    # echoes its input is not this gate's business.
    no_check = ast.parse("def _label(name: str) -> str:\n    return name\n")
    assert not _unchecked_parameter_return_offenders(no_check, synthetic)

    # Returning a derived value, or None, is what the contract allows.
    derived = ast.parse(
        "def _resolve(base: str, name: str) -> str | None:\n"
        "    path = (Path(base) / name).resolve()\n"
        "    path.relative_to(base)\n"
        "    return str(path)\n"
    )
    assert not _unchecked_parameter_return_offenders(derived, synthetic)


_HOST_POLICY_VALIDATORS = frozenset(
    {
        "canonical_page_identity",
        "filter_js_endpoints",
        "normalize_public_http_url",
        "public_url_or_none",
        "safe_public_url_or_none",
        "_validated_url",
    }
)


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


def _host_policy_url_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1].lower() in _HOST_POLICY_URL_CALLS or name.endswith(".SafeUrl.parse")


def _host_policy_validator_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1] in _HOST_POLICY_VALIDATORS or name.endswith(".SafeUrl.parse")


def _host_policy_value(node: ast.AST | None, policy_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in policy_names
    if isinstance(node, ast.Attribute) and node.attr in _HOST_POLICY_ATTRS:
        return isinstance(node.value, ast.Name) and node.value.id == "self"
    return False


def _host_policy_scope_names(tree: ast.AST, function: ast.AST) -> set[str]:
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
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
    if not _host_policy_validator_call(tree, node):
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
        return True, False
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and _reads_url_field(node.value, node.slice.value, url_names)
    ):
        return True, False
    if isinstance(node, ast.Call):
        if _host_policy_call_is_validated(tree, node, policy_names):
            return True, True
        if _host_policy_validator_call(tree, node):
            return True, False
        if _host_policy_url_call(tree, node):
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
    if isinstance(node, ast.Dict):
        return any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[0]
            for key, value in zip(node.keys, node.values, strict=True)
        ), any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[1]
            for key, value in zip(node.keys, node.values, strict=True)
        )
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
    policy_names = _host_policy_scope_names(tree, function)
    validated_names: set[str] = set()
    nodes = list(local_nodes(function))
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


def test_required_host_policy_detector_bites():
    bad = ast.parse("def normalize_public_http_url(url, *, policy=None):\n    return url\n")
    offenders = _required_host_policy_offenders(
        bad,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 1 and "HostPolicy" in offenders[0], offenders

    good = ast.parse(
        "from app.validation import public_url_or_none\n"
        "def normalize_public_http_url(url, *, policy: HostPolicy):\n"
        "    return public_url_or_none(url, policy=policy)\n"
    )
    assert not _required_host_policy_offenders(
        good,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    safe_bad = ast.parse("class SafeUrl:\n    def parse(self, url, *, policy=None):\n        return url\n")
    assert (
        len(
            _required_host_policy_offenders(
                safe_bad,
                REPO_ROOT / "app" / "_synthetic.py",
            )
        )
        == 1
    )
    # The private spelling. ``self._host_policy`` is what this codebase writes
    # everywhere, and the rule could not see it -- so a method validating through
    # the instance's own policy read as a method with no policy at all, and the
    # rule fired on it while staying silent on the one that really had none.
    via_instance = ast.parse(
        "from app.validation import public_url_or_none\n"
        "class JobStore:\n"
        "    def __init__(self, host_policy):\n"
        "        self._host_policy = host_policy\n"
        "    def canonical_root(self, root_url):\n"
        "        return str(public_url_or_none(root_url, policy=self._host_policy))\n"
    )
    assert not _required_host_policy_offenders(
        via_instance,
        REPO_ROOT / "app" / "_synthetic.py",
    ), "a method validating through self._host_policy read as having no policy"
    with_no_policy = ast.parse(
        "class JobStore:\n    def canonical_root(self, root_url):\n        return str(root_url)\n"
    )
    assert _required_host_policy_offenders(
        with_no_policy,
        REPO_ROOT / "app" / "_synthetic.py",
    ), "a method with no policy at all must still be caught"


_HOST_POLICY_PROPERTY = Property(
    name="host-policy",
    owner_module="app/validation.py",
    owner_symbols=("_host_policy_reason",),
    definition_site="app/validation.py:_host_policy_reason",
    fingerprints=("host_policy_validation",),
    reason="Every URL boundary must apply the host policy before forwarding a URL.",
)


def test_host_policy_detector_property_bites():
    assert_property_detector_bites(
        _HOST_POLICY_PROPERTY,
        lambda tree, path, _prop: _required_host_policy_offenders(tree, path),
        caught={
            "dotted urljoin import": (
                "from urllib.parse import urljoin\ndef f(root, path):\n    return urljoin(root, path)\n",
                1,
            ),
            "annotation without policy use": (
                "def f(url, *, policy: HostPolicy):\n    return url\n",
                1,
            ),
            "callable indirection without policy": (
                "from app.validation import public_url_or_none\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    validator = public_url_or_none\n"
                "    return validator(url)\n",
                1,
            ),
            "dotted urlunsplit import": (
                "from urllib.parse import urlunsplit\ndef f(parts):\n    return urlunsplit(parts)\n",
                1,
            ),
        },
        quiet={
            "dotted urljoin validated": (
                "from urllib.parse import urljoin\n"
                "from app.validation import public_url_or_none\n"
                "def f(root, path, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urljoin(root, path), policy=policy)\n"
            ),
            "annotation replaced by policy use": (
                "class Client:\n"
                "    policy: HostPolicy\n"
                "    def emit(self, url):\n"
                "        return public_url_or_none(url, policy=self.policy)\n"
            ),
            "callable indirection with policy": (
                "from app.validation import public_url_or_none as validate\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    return validate(url, policy=policy)\n"
            ),
            "dotted urlunsplit validated": (
                "from urllib.parse import urlunsplit\n"
                "from app.validation import public_url_or_none\n"
                "def f(parts, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urlunsplit(parts), policy=policy)\n"
            ),
        },
    )


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
    path = REPO_ROOT / "app" / "crawl" / "extraction.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not [
        offender
        for offender in _required_host_policy_offenders(tree, path)
        if any(name in offender for name in ("_extract_frames", "_extract_forms"))
    ]


def test_url_returning_helpers_require_host_policy():
    offenders = app_offenders(_required_host_policy_offenders)
    assert not offenders, ", ".join(offenders)


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
    assert len(attribute_calls) == 3
    assert all(resolved_call_name(source, call) == "ipaddress.ip_address" for call in attribute_calls)


# ---------------------------------------------------------------------------
# Normalize-or-None chokepoint (jscpd #5 family)
#
# The same three-line wrapper -- try normalize_public_http_url(x) except
# ValueError: return None -- was hand-rolled in three modules (search
# providers, map, batch dedupe). Each is a URL egress door that must apply
# the SSRF private-target policy; a new call site that skips the wrapper
# silently drops the policy for its door.
# ---------------------------------------------------------------------------

_NORMALIZE_OR_NONE_OFFENDERS = frozenset(
    {"normalize_public_http_url", "public_url_or_none", "safe_public_url_or_none"}
)


def _normalize_or_none_offenders(tree, path):
    """A try/except ValueError around the normalizer outside the chokepoint."""
    for node in try_blocks(tree):
        body_names = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in nodes_of(node)
            if isinstance(n, ast.Call)
        }
        if "normalize_public_http_url" not in body_names:
            continue
        for handler in node.handlers:
            if not (isinstance(handler.type, ast.Name) and handler.type.id == "ValueError"):
                continue
            # The re-inlined wrapper discards the exception: its body is a bare
            # None, pass, or a raw fallback (``x`` returned unchanged). A site
            # that re-raises, branches on the error text, or needs the
            # normalized value for a second computation (registrable_domain,
            # the error message) is a different contract and stays.
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
                    "normalize_public_http_url + except ValueError "
                    "re-inlined; use app.validation.safe_public_url_or_none",
                )


def test_normalize_or_none_is_not_reinlined():
    offenders = []
    # The chokepoint's own definition is the sanctioned site.
    for path, tree in app_trees(exclude=frozenset({"validation.py"})):
        offenders.extend(_normalize_or_none_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_normalize_or_none_detector_bites():
    assert_detector(
        _normalize_or_none_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def f(u):\n"
            "    try:\n"
            "        return normalize_public_http_url(u)\n"
            "    except ValueError:\n"
            "        return None\n": "safe_public_url_or_none",
        },
        clean=("def f(u):\n    return safe_public_url_or_none(u)\n",),
    )


# ---------------------------------------------------------------------------
# Unfiltered urljoin egress doors (H class)
#
# _detect_pagination read an href out of attacker-controlled HTML and returned
# urljoin(final_url, href) raw. The HTTP consumer filtered it before use, the
# browser consumer did not, so a javascript: rel=next reached the response.
# Filtering moved to the producer; this keeps the next door from reopening.
# ---------------------------------------------------------------------------

_EGRESS_FILTERS = frozenset({"public_url_or_none", "safe_public_url_or_none"})


def _is_urljoin_call(node: ast.expr) -> bool:
    """True for a plain ``urljoin(...)`` call.

    Takes the ``ast.expr`` its callers hold (an assignment's value, a return's
    value) rather than the ``ast.AST`` the walk produced, and narrows inside.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin"


def _unfiltered_urljoin_return_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a function that returns a ``urljoin`` result with no egress filter.

    Caught shapes: ``return urljoin(a, b)`` and ``u = urljoin(a, b); return u``.
    A URL resolved out of untrusted text is an egress door as soon as a caller
    trusts it, so the join and the filter belong together; the browser path
    proved that "every consumer remembers to filter" does not scale.
    ``app/safe_url.py`` composes ``urljoin`` inside the sanctioned
    ``SafeUrl.parse``/``redirect_hop`` path, so it is exempt by file, matching
    the other chokepoint-module exemptions.
    """
    if path.name == "safe_url.py":
        return []
    offenders: list[str] = []
    for func in functions(tree):
        joined: set[str] = set()
        returns_raw = False
        returns_binding = False
        for node in nodes_of(func):
            if isinstance(node, ast.Assign) and _is_urljoin_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        joined.add(target.id)
            elif isinstance(node, ast.Return):
                if _is_urljoin_call(node.value):
                    returns_raw = True
                elif isinstance(node.value, ast.Name) and node.value.id in joined:
                    returns_binding = True
        if not (returns_raw or returns_binding):
            continue
        uses_filter = any(
            (isinstance(n, ast.Name) and n.id in _EGRESS_FILTERS)
            or (isinstance(n, ast.Attribute) and n.attr in _EGRESS_FILTERS)
            for n in nodes_of(func)
        )
        if not uses_filter:
            offenders.append(
                offender_at(path, func, f"{func.name}() returns a urljoin with no egress filter")
            )
    return offenders


def test_no_unfiltered_urljoin_returns():
    """A URL joined from untrusted input is egress-filtered before it leaves."""
    assert_trees_clean(
        _unfiltered_urljoin_return_offenders,
        "filter joined URLs through public_url_or_none",
        trees=app_trees(),
    )


def test_unfiltered_urljoin_detector_bites():
    """The historical leak shape fires; the filtered and sanctioned shapes stay clean."""
    raw = ast.parse("def _detect_pagination(html, final_url):\n    return urljoin(final_url, href)\n")
    offenders = _unfiltered_urljoin_return_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "no egress filter" in offenders[0], offenders
    binding = ast.parse("def f(base, href):\n    u = urljoin(base, href)\n    return u\n")
    assert _unfiltered_urljoin_return_offenders(binding, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    good = ast.parse(
        "def f(href, final_url):\n    return public_url_or_none(href, resolve_against=final_url)\n"
    )
    assert not _unfiltered_urljoin_return_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # safe_url.py composes urljoin inside SafeUrl.parse / redirect_hop.
    hop = ast.parse(
        "def hop(self, location):\n    return SafeUrl.parse(urljoin(self.raw, location), pin=True)\n"
    )
    assert not _unfiltered_urljoin_return_offenders(hop, REPO_ROOT / "app" / "safe_url.py")


def _definitions_in(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The scope-compiler labels one parsed tree contributes.

    Split out from :func:`_scope_compiler_definitions` so the bites case can
    hand the detector a synthetic tree: the walk owns app/ only, so a test
    that wants to prove the detector fires has to plant the shape itself.
    """
    found: list[str] = []
    for node in nodes_of(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in _SCOPE_COMPILER_NAMES:
                found.append(offender_at(path, node, f"scope-regex compiler {node.name}"))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _SCOPE_PATTERN_LENGTH_BOUND
            for target in node.targets
        ):
            found.append(offender_at(path, node, "scope-regex length bound"))
    return found


def _scope_compiler_definitions() -> list[str]:
    """File labels of every scope-regex compiler and of its length bound."""
    found: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        found.extend(_definitions_in(tree, path))
    return found


def test_scope_regex_compiler_has_exactly_one_definition():
    """Behavior parity across the dedupe is covered by tests/crawl/test_scope_regex.py;
    this pins that no second copy of the compiler or of its bound can creep back."""
    found = _scope_compiler_definitions()
    compilers = [label for label in found if "compiler" in label]
    bounds = [label for label in found if "length bound" in label]
    assert len(compilers) == 1, f"scope-regex compiler duplicated again: {compilers}"
    assert len(bounds) == 1, f"scope-regex length bound duplicated again: {bounds}"
    assert "app/crawl/admission.py" in compilers[0], compilers
    assert "app/crawl/admission.py" in bounds[0], bounds


def test_scope_regex_compiler_detector_bites():
    """The detector reports one label per definition, so the tree it must
    reject is any tree with more than one -- two compilers, or the one
    compiler plus a second copy of its bound."""
    path = REPO_ROOT / "app" / "crawl" / "admission.py"
    two_compilers = ast.parse(
        "def compile_scope_pattern(value):\n    return value\n"
        "def _compile_scope_regex_safe(value):\n    return value\n"
    )
    compiler_and_bound = ast.parse(
        "_MAX_SCOPE_PATTERN_LEN = 256\ndef compile_scope_pattern(value):\n    return value\n"
    )
    assert len(_definitions_in(two_compilers, path)) == 2
    assert len(_definitions_in(compiler_and_bound, path)) == 2
    assert not _definitions_in(ast.parse("def _helper(value):\n    return value\n"), path)


def _frontier_expression(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "frontier"


# The admission chokepoint owns the pattern it bans: admit_job_links is the one
# function allowed to grow a frontier, so the sweep that bans direct growth
# everywhere else has to skip the module that defines it.
_ADMISSION_CHOKEPOINT = "app/crawl/admission.py"


def _direct_frontier_growth_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    allowed_functions: frozenset[str] = frozenset(),
) -> list[str]:
    growth_methods = {"append", "extend", "insert"}
    offenders: list[str] = []
    parents = {child: parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}

    def is_allowed(node: ast.AST) -> bool:
        if not allowed_functions:
            return False
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if (
            not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
            or owner.name not in allowed_functions
        ):
            return False
        if owner.name == "normalize_job_state":
            return (
                isinstance(node, ast.Assign)
                and any(_frontier_expression(target) for target in node.targets)
                and isinstance(node.value, ast.Name)
                and node.value.id == "frontier"
            )
        return (
            owner.name == "_persist"
            and isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and _frontier_expression(node.value)
        )

    for node in nodes_of(tree):
        if is_allowed(node):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in growth_methods and _frontier_expression(node.func.value):
                offenders.append(offender_at(path, node, "direct frontier growth bypasses admit_job_links"))
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_frontier_expression(target) for target in targets):
                offenders.append(offender_at(path, node, "frontier replacement bypasses admit_job_links"))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
                if _frontier_expression(node.value):
                    offenders.append(offender_at(path, node, "frontier alias bypasses admit_job_links"))
                if node.value.attr == "append" and _frontier_expression(node.value.value):
                    offenders.append(
                        offender_at(path, node, "frontier method alias bypasses admit_job_links")
                    )
        elif isinstance(node, ast.Subscript) and _frontier_expression(node.value):
            parent = parents.get(node)
            if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign):
                offenders.append(offender_at(path, node, "frontier slice mutation bypasses admit_job_links"))
    return offenders


def test_job_frontier_growth_only_uses_funnel():
    offenders = app_offenders(
        _direct_frontier_growth_offenders,
        skip=(_ADMISSION_CHOKEPOINT,),
        kwargs_for=lambda relative: (
            {"allowed_functions": frozenset({"normalize_job_state", "_persist"})}
            if relative == "app/crawl/job_store.py"
            else {}
        ),
    )
    assert not offenders, "Grow whole-site frontiers only through admit_job_links: " + ", ".join(offenders)


def test_job_frontier_growth_detector_bites():
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    pre_fix_owners = (
        "def run(job, url):\n    job.frontier.append(url)\n",
        "def run(job, url):\n    append = job.frontier.append\n    append(url)\n",
        "def run(job, url):\n    job.frontier += [url]\n",
        "def run(job, urls):\n    job.frontier = urls\n",
    )
    for source in pre_fix_owners:
        assert _direct_frontier_growth_offenders(ast.parse(source), path), source
    alias = ast.parse("def run(job, url):\n    frontier = job.frontier\n    frontier.append(url)\n")
    assert _direct_frontier_growth_offenders(alias, path)
    good = ast.parse("async def run(job, links):\n    await admit_job_links(job, links)\n")
    assert not _direct_frontier_growth_offenders(good, path)


_FILTER_NAMES = {
    "app.constants.is_analytics_host": "is_analytics_host",
    "app.constants.is_document_url": "is_document_url",
    "app.crawl.scope_match.scope_allows": "scope_allows",
    "app.validation.normalize_public_http_url": "normalize_public_http_url",
}


def _imported_filter_names(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes_of(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app.constants",
            "app.crawl.scope_match",
            "app.validation",
        }:
            for imported in node.names:
                canonical = f"{node.module}.{imported.name}"
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
        elif isinstance(node, ast.Import):
            for imported in node.names:
                canonical = imported.name
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
    return aliases


def _shared_admission_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    assert isinstance(tree, ast.Module)
    aliases = _imported_filter_names(tree)
    parents = {child: parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
    function_calls: dict[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] = {}
    filter_names = set(_FILTER_NAMES.values())
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = aliases.get(node.func.id, node.func.id)
        if name not in filter_names:
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            function_calls.setdefault(owner, set()).add(name)
    offenders: list[str] = []
    for function, names in function_calls.items():
        if len(names) >= 2:
            offenders.append(offender_at(path, function, "recursive admission filter list is re-inlined"))
    return offenders


def test_shared_admission_filter_set_has_no_twin_list():
    offenders = app_offenders(_shared_admission_filter_offenders, skip=(_ADMISSION_CHOKEPOINT,))
    assert not offenders, "Use the shared admission filter chokepoint: " + ", ".join(offenders)


def test_shared_admission_filter_detector_bites():
    assert_detector(
        _shared_admission_filter_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "def expand(links, visited, scope):\n"
            "    candidates = []\n"
            "    for link in links:\n"
            "        if link.url in visited: continue\n"
            "        if is_analytics_host(link.url): continue\n"
            "        if is_document_url(link.url): continue\n"
            "        if scope is not None and not scope_allows(scope, link.url): continue\n"
            "        candidates.append(link)\n"
            "    return candidates\n": "admission filter list is re-inlined",
        },
        clean=("async def expand(links):\n    return await admit_discovered_links(links)\n",),
    )


def test_job_frontier_detector_sanction_is_narrow():
    path = REPO_ROOT / "app" / "crawl" / "job_store.py"
    allowed = frozenset({"normalize_job_state", "_persist"})
    bad = ast.parse("def grow_frontier(job):\n    job.frontier.append('x')\n")
    assert _direct_frontier_growth_offenders(bad, path, allowed_functions=allowed)
    sanctioned = ast.parse("def normalize_job_state(job):\n    frontier = []\n    job.frontier = frontier\n")
    assert not _direct_frontier_growth_offenders(sanctioned, path, allowed_functions=allowed)
