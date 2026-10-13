"""Every URL reaches a sink through a named door.

Entry checks: dedup keys, body caps, scope matchers."""

import ast
import pathlib

from tests.chokepoint_walks import (
    SANCTIONED,
    app_trees,
    f_strings,
    functions,
    nodes_of,
    offender,
    offender_at,
)
from tests.rules import Bites, Rule

_CANONICALIZER = frozenset({"NormalizedUrl", "_validated_url", "admit_or_none"})


def _raw_batch_dedup_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``dict.fromkeys`` batch dedup that keys on raw values.

    The url-batch dedup at ``_payload_urls`` must collapse duplicates by
    admitted page identity (``NormalizedUrl`` admission) -- not the raw
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


BATCH_DEDUP_KEYS_ON_ADMITTED_IDENTITY = Rule(
    name="batch-dedup-keys-on-admitted-identity",
    find=_raw_batch_dedup_offenders,
    summary=(
        "The url-batch dedup must collapse duplicates by admitted page identity "
        "(NormalizedUrl admission), not the raw string, or a request carrying "
        "both .../x and .../x/?utm_source=tw crawls one page twice"
    ),
    bite=lambda: Bites(
        "def _payload_urls(payload):\n    return list(dict.fromkeys(payload['urls']))\n",
        "dedups a batch with dict.fromkeys, not the canonicalizer",
    ),
    clean=lambda: (
        "def _payload_urls(payload):\n"
        "    return [u for u in dict.fromkeys(payload['urls']) if _validated_url(u)]\n",
    ),
)


def test_batch_dedup_keys_on_admitted_identity():
    """URL-batch dedup must collapse by admitted identity, never raw strings."""
    BATCH_DEDUP_KEYS_ON_ADMITTED_IDENTITY.scan()


def test_batch_dedup_detector_bites():
    """A raw dict.fromkeys batch dedup fires; the canonicalized form stays clean."""
    BATCH_DEDUP_KEYS_ON_ADMITTED_IDENTITY.check_bites()


def _bare_body_reads(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an unbounded body acquisition on a response-like object.

    Both ``response.read()`` (no size arg) and ``response.content`` pull the
    whole body into RAM. Capped reads go through app.safety.bounded_io.read_capped_async;
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
        "body into RAM; use app.safety.bounded_io.read_capped_async instead"
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

    Scope note: app/safety/bounded_io.py IS the chokepoint and streams the capped body
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
    contract. Deduped into app/admission/scope_match.py -- a second definition
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
    assert "app/admission/scope_match.py" in defs[0], defs


def test_no_pinned_url_leaks_into_final_url():
    """A pinned fetch must never surface str(response.url) as final_url.

    build_pinned_request_async rewrites a plain-HTTP host to its resolved IP
    so httpx reports the IP; surfacing it in final_url leaks topology and
    keys the cookie jar by IP. The fetchers must use current_url instead.
    Tree-wide: any module may grow a fetcher, so the final_url sink is
    checked everywhere (search providers use response.url legitimately but
    never as a final_url, so they are not false positives).
    """
    offenders = []
    for path, tree in app_trees():
        for node in nodes_of(tree):
            # Final_url = str(response.url) (assignment)
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id == "final_url"]
                if targets and _is_response_url(node.value):
                    offenders.append(offender_at(path, node, "final_url = str(response.url)"))
            # HttpFetchResult(final_url=str(response.url)) (keyword)
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


def test_no_inline_pinned_url_composition():
    """The pinned connect URL -- IP literal, IPv6 bracketing, port, and Host
    header composition -- is ``build_pinned_request``'s job alone.

    Three divergent copies of that composition had already drifted into the
    tree (the retired ``SafeUrl.connect``, the http fetcher, the webhook
    builder); the copies disagreed on Host-port handling and all lacked IPv6
    bracketing, which is why the composition lives in exactly one function.
    An f-string that mentions a pinned IP outside ``pinned_transport.py``
    re-opens that drift.
    """
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset({"pinned_transport.py", "safe_regex.py"})):
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
    assert not offenders, "compose pinned URLs via build_pinned_request(), never inline:\n" + "\n".join(
        offenders
    )
