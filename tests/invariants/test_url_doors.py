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
    app_trees,
    assignment_targets,
    callable_defs,
    enclosing_function,
    f_strings,
    functions,
    nodes_of,
    offender,
    offender_at,
    parent_map,
    resolved_call_name,
    returns,
    try_blocks,
)
from tests.invariants._shared import (
    _SCOPE_COMPILER_NAMES,
    _SCOPE_PATTERN_LENGTH_BOUND,
    function_defs,
)
from tests.rules import Bites, Rule, Silent

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


REDIRECT_HOPS_GO_THROUGH_SAFEURL_REDIRECT_HOP = Rule(
    name="redirect-hops-go-through-safeurl-redirect-hop",
    find=_hand_rolled_redirect_hops,
    summary=(
        "A hand-rolled redirect hop is the 'forgot one of the five steps' class "
        "SafeUrl exists to kill; use SafeUrl.redirect_hop (or the pinned fetch "
        "helper) rather than a raw urljoin combined with a 3xx status test"
    ),
    excludes=SANCTIONED | {"safe_url.py"},
    bite=lambda: Bites(
        "def hop(location, base):\n    if code in (301, 302):\n        return urljoin(base, location)\n",
        "hop()",
    ),
    clean=lambda: ("def link(href, base):\n    return SafeUrl.redirect_hop(href, base)\n",),
)


def test_no_hand_rolled_redirect_hops():
    """Redirect hops anywhere go through SafeUrl.redirect_hop (or the pinned
    fetch helper), never a raw urljoin combined with a 3xx status test."""
    REDIRECT_HOPS_GO_THROUGH_SAFEURL_REDIRECT_HOP.scan()


def test_redirect_hops_detector_bites():
    """A urljoin that tests a redirect status and skips the chokepoint fires."""
    REDIRECT_HOPS_GO_THROUGH_SAFEURL_REDIRECT_HOP.check_bites()


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


BATCH_DEDUP_KEYS_ON_CANONICAL_PAGE_IDENTITY = Rule(
    name="batch-dedup-keys-on-canonical-page-identity",
    find=_raw_batch_dedup_offenders,
    summary=(
        "The url-batch dedup must collapse duplicates by canonical page identity "
        "(normalize_public_http_url), not the raw string, or a request carrying "
        "both .../x and .../x/?utm_source=tw crawls one page twice"
    ),
    bite=lambda: Bites(
        "def _payload_urls(payload):\n    return list(dict.fromkeys(payload['urls']))\n",
        "dedups a batch with dict.fromkeys, not the canonicalizer",
    ),
    clean=lambda: (
        "def _payload_urls(payload):\n"
        "    return [u for u in payload['urls'] if normalize_public_http_url(u) not in seen]\n",
    ),
)


def test_batch_dedup_keys_on_canonical_page_identity():
    """URL-batch dedup must collapse by ``normalize_public_http_url``, never raw strings."""
    BATCH_DEDUP_KEYS_ON_CANONICAL_PAGE_IDENTITY.scan()


def test_batch_dedup_detector_bites():
    """A raw dict.fromkeys batch dedup fires; the canonicalized form stays clean."""
    BATCH_DEDUP_KEYS_ON_CANONICAL_PAGE_IDENTITY.check_bites()


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


EVERY_HTTP_BODY_READ_IS_CAPPED = Rule(
    name="every-http-body-read-is-capped",
    find=_bare_body_reads,
    summary=(
        "A response.read() with no size arg, or response.content, pulls the whole "
        "body into RAM; use app.bounded_io.read_capped_async instead"
    ),
    excludes=SANCTIONED | {"bounded_io.py"},
    bite=lambda: (
        Bites("def f(resp):\n    return resp.read()\n", ".read()"),
        Bites("def f(resp):\n    return resp.content\n", ".content"),
    ),
    clean=lambda: ("def f(resp):\n    return read_capped_async(resp)\n",),
)


def test_no_bare_body_reads():
    """Every HTTP body read anywhere must be capped ().

    Scope note: app/bounded_io.py IS the chokepoint and streams the capped body
    itself -- exempt by receiver, not path, so it survives a tree-wide sweep.
    Widening kept precise on the response-object convention to avoid flagging
    file/socket reads.
    """
    EVERY_HTTP_BODY_READ_IS_CAPPED.scan()


def test_bare_body_read_detector_bites():
    """Both unbounded read() and .content fire on a response-like receiver."""
    EVERY_HTTP_BODY_READ_IS_CAPPED.check_bites()


def _scope_matcher_definitions() -> list[str]:
    """File labels of every ``def scope_allows``/``_scope_allows`` under app/.

    The bounded scope check was verbatim-copied into engine.py and
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
            # Final_url = str(response.url)  (assignment)
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

    An unpinned parse is the rebinding window: validation resolves one IP,
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


SAFE_URL_PARSE_ALWAYS_PINS = Rule(
    name="safe-url-parse-always-pins",
    find=_unpinned_safe_url_parses,
    summary=(
        "An unpinned SafeUrl.parse is the DNS-rebinding window: validation resolves "
        "one IP and connect re-resolves at open time, so every call site must pass "
        "an explicit pin=True"
    ),
    excludes=SANCTIONED | {"safe_url.py"},
    bite=lambda: Bites(
        "def f(url):\n    return SafeUrl.parse(url)\n",
        "must pass pin=True",
    ),
    clean=lambda: ("def f(url):\n    return SafeUrl.parse(url, pin=True)\n",),
)


def test_safe_url_parse_always_pins():
    SAFE_URL_PARSE_ALWAYS_PINS.scan()


def test_safe_url_parse_detector_bites():
    """A parse without an explicit pin fires; the pinned form stays clean."""
    SAFE_URL_PARSE_ALWAYS_PINS.check_bites()


def test_no_inline_pinned_url_composition():
    """The pinned connect URL -- IP literal, IPv6 bracketing, port, and Host
    header composition -- is SafeUrl.connect()'s job alone.

    Three divergent copies of that composition had already drifted into the
    tree (safe_url, http fetcher, webhook builder); the copies disagreed on
    Host-port handling and all lacked IPv6 bracketing. An f-string
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
                        offenders.append(offender_at(path, node, "f-string interpolates.pinned_ip"))
                    if (
                        isinstance(expr, ast.Name)
                        and isinstance(expr.ctx, ast.Load)
                        and "pinned" in expr.id.lower()
                    ):
                        offenders.append(offender_at(path, node, f"f-string interpolates {expr.id}"))
    assert not offenders, "compose pinned URLs via SafeUrl.connect(), never inline:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Frontier raw-href sinks ()
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
        # A default on a *positional* HostPolicy is caught too, not just the
        # keyword-only form the mutations used.
        Bites(
            "def g(policy: HostPolicy = HostPolicy((), ())):\n    pass\n",
            "is a HostPolicy and is optional",
        ),
    ),
    clean=lambda: (
        # The required form is left alone, and so is a required parameter the rule
        # does not key off -- it is the annotation that matters, not the name.
        "async def h(url, *, policy: HostPolicy, host_policy: HostPolicy, other: str = 'x'):\n"
        "    return url\n",
    ),
)


def test_the_host_policy_is_never_optional():
    HOST_POLICY_IS_NEVER_OPTIONAL.scan()


def test_optional_host_policy_detector_bites():
    """A defaulted keyword or positional HostPolicy fires; required stays clean."""
    HOST_POLICY_IS_NEVER_OPTIONAL.check_bites()


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


def test_no_raw_href_frontier_sinks():
    """The frontier only ever carries frontier_link_url()-validated URLs."""
    FRONTIER_CARRIES_ONLY_LINK_VALIDATED_URLS.scan()


def test_raw_href_frontier_detector_bites():
    """Prove the detector fires on the historical shape."""
    FRONTIER_CARRIES_ONLY_LINK_VALIDATED_URLS.check_bites()


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
        # Without a containment check there is nothing to bypass, so a function that
        # echoes its input is not this gate's business.
        "def _label(name: str) -> str:\n    return name\n",
        # Returning a derived value, or None, is what the contract allows.
        "def _resolve(base: str, name: str) -> str | None:\n"
        "    path = (Path(base) / name).resolve()\n"
        "    path.relative_to(base)\n"
        "    return str(path)\n",
    ),
)


def test_path_validators_never_return_an_unchecked_parameter():
    PATH_VALIDATORS_RETURN_AN_ALLOWLISTED_VALUE_OR_NONE.scan()


def test_unchecked_parameter_return_detector_bites():
    """The allowlist-vs-return drift shape fires; fixed, derived and None stay clean."""
    PATH_VALIDATORS_RETURN_AN_ALLOWLISTED_VALUE_OR_NONE.check_bites()


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
# Normalize-or-None chokepoint
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


NORMALIZE_OR_NONE_IS_NOT_REINLINED = Rule(
    name="normalize-or-none-is-not-reinlined",
    find=_normalize_or_none_offenders,
    summary=(
        "The same three-line try/except ValueError wrapper around a normalizer was "
        "hand-rolled in three modules, each silently dropping the SSRF policy for "
        "its door; use app.validation.safe_public_url_or_none instead"
    ),
    # The chokepoint's own definition is the sanctioned site.
    scope=lambda *, exclude: app_trees(exclude=frozenset({"validation.py"})),
    bite=lambda: Bites(
        "def f(u):\n"
        "    try:\n"
        "        return normalize_public_http_url(u)\n"
        "    except ValueError:\n"
        "        return None\n",
        "safe_public_url_or_none",
    ),
    clean=lambda: ("def f(u):\n    return safe_public_url_or_none(u)\n",),
)


def test_normalize_or_none_is_not_reinlined():
    # The chokepoint's own definition is the sanctioned site.
    NORMALIZE_OR_NONE_IS_NOT_REINLINED.scan()


def test_normalize_or_none_detector_bites():
    NORMALIZE_OR_NONE_IS_NOT_REINLINED.check_bites()


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
            if isinstance(node, ast.Assign) and node.value is not None and _is_urljoin_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        joined.add(target.id)
            elif isinstance(node, ast.Return) and node.value is not None:
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


JOINED_URLS_ARE_EGRESS_FILTERED = Rule(
    name="joined-urls-are-egress-filtered",
    find=_unfiltered_urljoin_return_offenders,
    summary=(
        "A URL resolved out of untrusted text becomes an egress door as soon as a "
        "caller trusts it, so the join and the filter belong together; filter "
        "joined URLs through public_url_or_none"
    ),
    bite=lambda: (
        Bites(
            "def _detect_pagination(html, final_url):\n    return urljoin(final_url, href)\n",
            "no egress filter",
        ),
        # A urljoin bound to a name and returned is the same leak.
        Bites(
            "def f(base, href):\n    u = urljoin(base, href)\n    return u\n",
            "no egress filter",
        ),
    ),
    clean=lambda: (
        "def f(href, final_url):\n    return public_url_or_none(href, resolve_against=final_url)\n",
        # Safe_url.py composes urljoin inside SafeUrl.parse / redirect_hop, so the
        # join is sanctioned there and must fire at every other module.
        Silent(
            "def hop(self, location):\n    return SafeUrl.parse(urljoin(self.raw, location), pin=True)\n",
            at=REPO_ROOT / "app" / "safe_url.py",
        ),
    ),
)


def test_no_unfiltered_urljoin_returns():
    """A URL joined from untrusted input is egress-filtered before it leaves."""
    JOINED_URLS_ARE_EGRESS_FILTERED.scan()


def test_unfiltered_urljoin_detector_bites():
    """The historical leak shape fires; the filtered and sanctioned shapes stay clean."""
    JOINED_URLS_ARE_EGRESS_FILTERED.check_bites()


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
    parents = parent_map(tree)

    def is_allowed(node: ast.AST) -> bool:
        if not allowed_functions:
            return False
        owner = enclosing_function(node, parents)
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
            targets = assignment_targets(node)
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
            parent = parents.get(id(node))
            if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign):
                offenders.append(offender_at(path, node, "frontier slice mutation bypasses admit_job_links"))
    return offenders


JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL = Rule(
    name="job-frontier-growth-only-uses-funnel",
    find=_direct_frontier_growth_offenders,
    summary=(
        "Grow whole-site frontiers only through admit_job_links: a direct append, "
        "replacement, alias or slice mutation elsewhere re-opens the funnel every "
        "other entry point is collapsed through"
    ),
    skip=frozenset({_ADMISSION_CHOKEPOINT}),
    kwargs_for=lambda relative: (
        {"allowed_functions": frozenset({"normalize_job_state", "_persist"})}
        if relative == "app/crawl/job_store.py"
        else {}
    ),
    proof_path=REPO_ROOT / "app" / "api" / "job_runner.py",
    bite=lambda: (
        Bites(
            "def run(job, url):\n    job.frontier.append(url)\n",
            "direct frontier growth bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    append = job.frontier.append\n    append(url)\n",
            "frontier method alias bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    job.frontier += [url]\n",
            "frontier replacement bypasses admit_job_links",
        ),
        Bites(
            "def run(job, urls):\n    job.frontier = urls\n",
            "frontier replacement bypasses admit_job_links",
        ),
        Bites(
            "def run(job, url):\n    frontier = job.frontier\n    frontier.append(url)\n",
            "frontier alias bypasses admit_job_links",
        ),
    ),
    clean=lambda: ("async def run(job, links):\n    await admit_job_links(job, links)\n",),
)


def test_job_frontier_growth_only_uses_funnel():
    JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL.scan()


def test_job_frontier_growth_detector_bites():
    JOB_FRONTIER_GROWTH_ONLY_USES_FUNNEL.check_bites()


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
    parents = parent_map(tree)
    function_calls: dict[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] = {}
    filter_names = set(_FILTER_NAMES.values())
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = aliases.get(node.func.id, node.func.id)
        if name not in filter_names:
            continue
        owner = enclosing_function(node, parents)
        if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            function_calls.setdefault(owner, set()).add(name)
    offenders: list[str] = []
    for function, names in function_calls.items():
        if len(names) >= 2:
            offenders.append(offender_at(path, function, "recursive admission filter list is re-inlined"))
    return offenders


SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST = Rule(
    name="shared-admission-filter-set-has-no-twin-list",
    find=_shared_admission_filter_offenders,
    summary=(
        "The is_analytics_host / is_document_url / scope_allows filter ladder is one "
        "shared list; a function re-inlining more than one of them by hand is a "
        "twin that drifts from the chokepoint\u2019s future edits"
    ),
    skip=frozenset({_ADMISSION_CHOKEPOINT}),
    bite=lambda: Bites(
        "def expand(links, visited, scope):\n"
        "    candidates = []\n"
        "    for link in links:\n"
        "        if link.url in visited: continue\n"
        "        if is_analytics_host(link.url): continue\n"
        "        if is_document_url(link.url): continue\n"
        "        if scope is not None and not scope_allows(scope, link.url): continue\n"
        "        candidates.append(link)\n"
        "    return candidates\n",
        "recursive admission filter list is re-inlined",
    ),
    clean=lambda: ("async def expand(links):\n    return await admit_discovered_links(links)\n",),
)


def test_shared_admission_filter_set_has_no_twin_list():
    SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST.scan()


def test_shared_admission_filter_detector_bites():
    SHARED_ADMISSION_FILTER_SET_HAS_NO_TWIN_LIST.check_bites()


def test_job_frontier_detector_sanction_is_narrow():
    path = REPO_ROOT / "app" / "crawl" / "job_store.py"
    allowed = frozenset({"normalize_job_state", "_persist"})
    bad = ast.parse("def grow_frontier(job):\n    job.frontier.append('x')\n")
    assert _direct_frontier_growth_offenders(bad, path, allowed_functions=allowed)
    sanctioned = ast.parse("def normalize_job_state(job):\n    frontier = []\n    job.frontier = frontier\n")
    assert not _direct_frontier_growth_offenders(sanctioned, path, allowed_functions=allowed)
