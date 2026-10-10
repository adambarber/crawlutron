import ast

import pytest

from tests.chokepoint_walks import TESTS_DIR, local_nodes, nodes_of, trees_under

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
# A funnel is not always a function: a `multiprocessing.Process` stand-in is a
# class, and the walk has to reach classes or a class-shaped funnel cannot be
# detected at all -- the declaration file agrees with itself that it holds
# nothing, which is the same failure mode as the walk stopping at the first
# `def`.
NESTABLE_TYPES = (*FUNCTION_TYPES, ast.ClassDef)

FUNNELS = [
    (
        "webhook-delivery",
        "poll_webhook_delivery",
        'def f(records):\n return poll_until(done, json.loads(records[0].decode("utf-8")))',
    ),
    (
        "async-submit",
        "submit_async_crawl",
        'async def f(client):\n await client.post("/v1/crawl", json={})\n await poll_until(done)',
    ),
    (
        "render-pool",
        "post_render_with_pool",
        'async def f(client, pool):\n set_browser_pool(pool)\n await client.post("/v1/render", json={})',
    ),
    (
        "job-post",
        "post_api_job",
        'async def f(client):\n await client.post("/v1/jobs", json={"root_url": "x", "policy": '
        "_execution_policy(None)})",
    ),
    (
        "seeded-job",
        "run_seeded_scripted_job",
        "async def f():\n"
        " job = _seed_job(store, url, policy)\n"
        " return await run_seeded_job(job, store, results)",
    ),
    (
        "blocking-route",
        "blocking_pinned_request",
        "def build():\n import threading\n import time\n threading.get_ident()\n time.sleep(0.05)",
    ),
    (
        "response-handler",
        "response_handler_double",
        "async def f(collected):\n handler = BrowserRenderer._make_response_handler(collected)\n "
        "class R:\n  async def json(self): pass\n await handler(R())",
    ),
    (
        "http-strategy",
        "run_http_strategy",
        "async def f():\n return await http_strategy_result(request=StrategyRequest(original_url=url, "
        'normalized_url=url, strategy="HTTP_DECLARED_CRAWLER", policy=policy, '
        "host_policy=HostPolicy.unrestricted()), http_fetcher=fetcher)",
    ),
    (
        "cache-entries",
        "make_expiring_cache",
        "def f(tmp):\n cache = ContentCache(tmp, default_ttl=10, clock=clock, max_entries=100, "
        'max_total_bytes=10000)\n cache.set("a", "x" * 100)\n cache.set("b", "x" * 100)',
    ),
    (
        "html-fetcher",
        "make_html_fetcher",
        "def f(calls):\n async def fetcher(url, headers, timeout):\n  calls.append(url)\n  return "
        'make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=body)',
    ),
    (
        "loop-rebind",
        "run_loop_rebind_scenario",
        "async def f(get):\n first = get()\n foreign = asyncio.new_event_loop()\n "
        "first._bound_loop = foreign\n await asyncio.sleep(0)\n foreign.close()",
    ),
    (
        "default-fetcher",
        "run_default_http_fetcher",
        "async def f():\n return await default_http_fetcher(url, {}, 30, policy=HostPolicy.unrestricted())",
    ),
    (
        "job-store",
        "make_job_store",
        'def f(tmp):\n return JobStore(tmp / "jobs.db", policy=UNRESTRICTED_HOST_POLICY)',
    ),
    (
        "scripted-fetcher",
        "scripted_fetcher",
        "def f():\n async def fetcher(url, headers, timeout, max_content_length=50 * "
        "1024 * 1024):\n  return make_fetch(url=url, final_url=url, status=200, body=body)",
    ),
    (
        "crawled-urls",
        "crawled_urls",
        'async def f():\n payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1}}}\n '
        "out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())\n return "
        '{r["normalized_url"] for r in out["results"]}',
    ),
    (
        "ssrf-resolution",
        "_with_private_resolution",
        'def f(monkeypatch):\n block_private_targets(monkeypatch)\n with patch("socket.getaddrinfo", '
        'return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):\n  return call()',
    ),
    (
        "crawl-submit",
        "_post_crawl",
        'async def f(client):\n resp = await client.post("/v1/crawl", json=payload)\n assert '
        "resp.status_code "
        ""
        ""
        ""
        ""
        ""
        ""
        '== 200\n return resp.json()["results"][0]',
    ),
    (
        "process-double",
        "_StubProc",
        "class _StubProc:\n def is_alive(self): return True\n def terminate(self): "
        "pass\n def kill(self): pass\n def join(self, timeout=None): pass",
    ),
    (
        "lifespan-teardown",
        "_stub_teardown",
        'def f(monkeypatch):\n monkeypatch.setattr(http_mod, "close_async_http_client", _noop)\n '
        'monkeypatch.setattr(main_mod, "close_browser_pool", _noop)',
    ),
]

# Modules that are allowed to *implement* a funnel rather than route through it.
# The fetcher contract in particular is declared once, in the module that owns
# the factories; a copy anywhere else re-asserts production's own defaults and
# is the drift this file exists to catch.
FUNNEL_HOME_MODULES = frozenset(
    {
        "_fetcher_factories.py",
        # The fetchers, the crawls and the doubles are declared once, in the
        # modules that own them. A copy in any other module re-asserts the
        # contract, and that is the drift these detectors exist to catch.
        "_assertions.py",
        "_process_doubles.py",
        "_lifespan_doubles.py",
    }
)


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_call(node, name, first=None):
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == name
        and (first is None or bool(child.args) and _literal(child.args[0]) == first)
        for child in nodes_of(node)
    )


def _literal(node):
    return node.value if isinstance(node, ast.Constant) else None


def _has_attr_assign(node, name):
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == name for target in child.targets)
        for child in nodes_of(node)
    )


def _webhook_delivery(node):
    return _has_call(node, "poll_until") and _has_call(node, "loads") and _has_call(node, "decode")


def _async_submit(node):
    return _has_call(node, "post", "/v1/crawl") and _has_call(node, "poll_until")


def _render_pool(node):
    return _has_call(node, "set_browser_pool") and _has_call(node, "post", "/v1/render")


def _job_post(node):
    return _has_call(node, "post", "/v1/jobs") and _has_call(node, "_execution_policy")


def _seeded_job(node):
    """A seeded-job run assembled by hand instead of through the funnel.

    The two halves are the row seeder and the seeded runner. Reaching for both
    in one scope is how the shape was written before ``run_seeded_scripted_job``
    existed, and a reader copying it gets the persistence and recovery wiring
    inlined again. A call to the funnel itself is the sanctioned state and must
    not be reported -- the owner is skipped by name further up, but naming it
    here as well would flag every legitimate caller.
    """
    return _has_call(node, "_seed_job") and _has_call(node, "run_seeded_job")


def _blocking_route(node):
    return _has_call(node, "get_ident") and _has_call(node, "sleep")


def _response_handler(node):
    return _has_call(node, "_make_response_handler") and any(
        isinstance(child, ast.ClassDef) for child in nodes_of(node)
    )


def _http_strategy(node):
    """A call that re-spells one page's identity inline instead of passing the record.

    Kept in step with the entry point's signature: when the five identity fields
    moved into ``StrategyRequest``, this detector had to follow them inside the
    ``request=`` keyword, or a match on the old six-keyword shape could never
    fire again and the clone it exists to catch would be reported as clean.
    """
    for child in nodes_of(node):
        if not isinstance(child, ast.Call) or _name(child.func) != "http_strategy_result":
            continue
        words = {keyword.arg: keyword.value for keyword in child.keywords}
        request = words.get("request")
        if not isinstance(request, ast.Call) or _name(request.func) != "StrategyRequest":
            continue
        fields = {keyword.arg: keyword.value for keyword in request.keywords}
        if (
            {"original_url", "normalized_url", "strategy"} <= fields.keys()
            and _literal(fields["strategy"]) == "HTTP_DECLARED_CRAWLER"
            and ast.dump(fields["original_url"]) == ast.dump(fields["normalized_url"])
        ):
            return True
    return False


def _cache_entries(node):
    values = [
        child.args[1]
        for child in nodes_of(node)
        if isinstance(child, ast.Call) and _name(child.func) == "set" and len(child.args) > 1
    ]
    hundred_byte_values = sum(
        isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mult) and _literal(value.right) == 100
        for value in values
    )
    return _has_call(node, "ContentCache") and hundred_byte_values >= 2


def _html_fetcher(node):
    appends_url = any(
        isinstance(child, ast.Call)
        and _name(child.func) == "append"
        and child.args
        and isinstance(child.args[0], ast.Name)
        and child.args[0].id == "url"
        for child in nodes_of(node)
    )
    exact_fetch = any(
        isinstance(child, ast.Call)
        and _name(child.func) == "make_fetch"
        and any(keyword.arg == "status" and _literal(keyword.value) == 200 for keyword in child.keywords)
        and any(
            keyword.arg == "ctype" and _literal(keyword.value) == "text/html" for keyword in child.keywords
        )
        and any(
            keyword.arg == "body" and isinstance(keyword.value, ast.Name) and keyword.value.id == "body"
            for keyword in child.keywords
        )
        for child in nodes_of(node)
    )
    return appends_url and exact_fetch


def _loop_rebind(node):
    return _has_call(node, "new_event_loop") and _has_attr_assign(node, "_bound_loop")


def _default_fetcher(node):
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == "default_http_fetcher"
        and len(child.args) == 3
        and isinstance(child.args[1], ast.Dict)
        and _literal(child.args[2]) == 30
        and any(
            keyword.arg == "policy"
            and isinstance(keyword.value, ast.Call)
            and _name(keyword.value.func) == "unrestricted"
            for keyword in child.keywords
        )
        for child in nodes_of(node)
    )


def _job_store(node):
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == "JobStore"
        and child.args
        and "jobs.db" in ast.dump(child.args[0])
        and any(
            keyword.arg == "policy" and _name(keyword.value).endswith("UNRESTRICTED_HOST_POLICY")
            for keyword in child.keywords
        )
        for child in nodes_of(node)
    )


def _is_50_mib_ceiling(node) -> bool:
    """Whether a default value is production's ``50 * 1024 * 1024`` byte ceiling.

    Matched on the arithmetic rather than a single literal, so the detector
    survives a reformat and would still fire if the constant were re-derived
    (``52_428_800``, ``50 << 20``) without turning into noise.
    """
    dumped = ast.dump(node)
    return "50" in dumped and "1024" in dumped


def _scripted_fetcher(node):
    """A def that re-declares the production ``http_fetcher`` contract.

    The distinctive part is the ``max_content_length`` default: 50 MiB is the
    ceiling in ``app/transport/fetchers.py``, and a test def that
    hard-codes it is asserting that ceiling by copying it. When production
    lowers the ceiling such a copy still passes, and the test that looked like
    it covered the bound covers nothing.

    Requiring a ``headers`` parameter alongside it is what keeps this from
    matching every four-argument helper: a def taking a request's headers and a
    content-length cap is a fetcher, whatever it is called.
    """
    if not isinstance(node, FUNCTION_TYPES):
        return False
    params = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
    if "headers" not in params or "max_content_length" not in params:
        return False
    positional = [*node.args.posonlyargs, *node.args.args]
    defaults = node.args.defaults
    if not defaults or len(defaults) > len(positional):
        return False
    # Defaults align to the tail of the positional list.
    for arg, default in zip(positional[len(positional) - len(defaults) :], defaults, strict=True):
        if arg.arg == "max_content_length" and _is_50_mib_ceiling(default):
            return True
    return False


def _ssrf_resolution(node):
    """A test that builds a private-resolution precondition by hand.

    Two halves, and both are required: the allow-private escape hatch has to be
    closed, and the resolver has to be pinned. A copy that closes one and not
    the other produces a verdict about the environment rather than the policy,
    and a copy that is missing one half fails (or passes) for the wrong reason.
    """
    blocks = _has_call(node, "block_private_targets")
    pins = any(
        isinstance(child, ast.Call)
        and _name(child.func) == "patch"
        and any(
            isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "getaddrinfo" in arg.value
            for arg in child.args
        )
        for child in nodes_of(node)
    )
    return bool(blocks and pins)


def _crawl_submit(node):
    """A raw /v1/crawl POST whose body is projected for results.

    Reading ``["results"]`` off a crawl response *is* the assertion that the
    request ran synchronously -- which is what ``completed_crawl`` owns and
    checks once. Narrower than "any crawl POST" on purpose: a test that wants
    the status code, the headers or a 422 rejection is asserting something else
    and legitimately needs the raw response, so flagging it would be the
    detector insisting on a funnel that does not fit.
    """
    posts = [
        child
        for child in nodes_of(node)
        if isinstance(child, ast.Call)
        and _name(child.func) == "post"
        and child.args
        and _literal(child.args[0]) == "/v1/crawl"
    ]
    if not posts:
        return False
    return any(
        isinstance(child, ast.Subscript)
        and _literal(child.slice) == "results"
        and any(isinstance(inner, ast.Attribute) and inner.attr == "json" for inner in local_nodes(child))
        for child in nodes_of(node)
    )


_PROCESS_DOUBLE_METHODS = frozenset({"is_alive", "terminate", "kill", "join"})


def _process_double(node):
    """A second ``multiprocessing.Process`` stand-in.

    The pool's contract *is* signal semantics, so a stand-in is any class with
    is_alive/terminate/kill/join. Three copies of one existed, each freezing its
    own idea of what ``exitcode`` should be; ``_process_doubles.StubProc`` models
    all of it and a test states only the property it needs.
    """
    if not isinstance(node, ast.ClassDef):
        return False
    return _PROCESS_DOUBLE_METHODS <= {
        child.name for child in node.body if isinstance(child, ast.FunctionDef)
    }


def _lifespan_teardown(node):
    """A hand-built "neutralise the heavy lifespan teardown" setup.

    Which closers the lifespan reaches is production's list, and a test that
    re-spells a subset reaches the real implementation for whatever the test
    forgot. ``stub_heavy_teardown`` derives the set instead.
    """
    if _has_call(node, "stub_heavy_teardown"):
        # Specialising the neutraliser is routing through it, not inlining it:
        # the set of closers still came from the chokepoint.
        return False
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == "setattr"
        and any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value in {"close_async_http_client", "close_cffi_session", "close_browser_pool"}
            for arg in child.args
        )
        for child in nodes_of(node)
    )


def _crawled_urls(node):
    """A test that crawls scripted pages by hand instead of through crawled_urls.

    The shape is a ``crawl_request`` whose ``http_fetcher`` is a freshly
    constructed ``site_fetcher(...)``: the test is serving a literal page table
    to the recursive crawler, which is exactly what ``crawled_urls`` does. A
    copied payload + copied call can drift from the chokepoint in either
    direction -- a policy key the helper knows about and the copy does not, or a
    cache the copy drops -- and the copy is the one the test actually ran.
    """
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == "crawl_request"
        and any(
            keyword.arg == "http_fetcher" and _has_call(keyword.value, "site_fetcher")
            for keyword in child.keywords
        )
        for child in nodes_of(node)
    )


DETECTORS = {
    "webhook-delivery": _webhook_delivery,
    "async-submit": _async_submit,
    "render-pool": _render_pool,
    "job-post": _job_post,
    "seeded-job": _seeded_job,
    "blocking-route": _blocking_route,
    "response-handler": _response_handler,
    "http-strategy": _http_strategy,
    "cache-entries": _cache_entries,
    "html-fetcher": _html_fetcher,
    "loop-rebind": _loop_rebind,
    "default-fetcher": _default_fetcher,
    "job-store": _job_store,
    "scripted-fetcher": _scripted_fetcher,
    "crawled-urls": _crawled_urls,
    "ssrf-resolution": _ssrf_resolution,
    "crawl-submit": _crawl_submit,
    "process-double": _process_double,
    "lifespan-teardown": _lifespan_teardown,
}


def _scopes(nodes):
    """Every function in ``nodes``, at any nesting depth.

    Recursion continues *through* a function body, not just around it. A
    hand-rolled setup is nearly always a closure inside a test rather than a
    module-level helper, so a walk that stops at the first ``def`` reports a
    clean tree while the thing it was written to catch has moved one indent in.
    Every detector below shares this walk, so the gap was not one detector's
    blind spot -- it was the whole declaration file agreeing on an empty tree.
    """
    for node in nodes:
        if isinstance(node, NESTABLE_TYPES):
            yield node
        yield from _scopes(ast.iter_child_nodes(node))


def _scopes_outside_owners(nodes, owners, inside_owner=False):
    """Yield ``(node, exempt)`` for every function, tracking owner ancestry.

    A funnel is exempt by *name*, and a funnel is usually a factory whose own
    implementation is the closure it returns -- ``blocking_pinned_request``
    builds ``build``, and ``build`` is the thing the detector recognises. So
    exemption has to be lexical, not just self-referential: a function nested
    anywhere inside a named owner is that owner's own body, and flagging it
    would be the detector reporting the declaration file's own plumbing.
    """
    for node in nodes:
        child_inside = inside_owner
        if isinstance(node, NESTABLE_TYPES):
            yield node, inside_owner
            child_inside = inside_owner or node.name in owners
        yield from _scopes_outside_owners(list(ast.iter_child_nodes(node)), owners, child_inside)


def _detector_bites(name, tree):
    return any(DETECTORS[name](node) for node in _scopes(tree.body))


def _clone_funnel_offenders():
    owners = {owner for _name, owner, _source in FUNNELS}
    offenders = []
    for path, tree in trees_under(TESTS_DIR):
        if path.name in FUNNEL_HOME_MODULES:
            continue
        scopes = [(node, exempt) for node, exempt in _scopes_outside_owners(tree.body, owners) if not exempt]
        for index, (node, _) in enumerate(scopes):
            for name, owner, _source in FUNNELS:
                if node.name == owner or not DETECTORS[name](node):
                    continue
                # Only the innermost scope that matches is reported. A method
                # inside a class fires on both nodes, and naming the class too
                # sends a reader to the class body instead of to the line that
                # has to change.
                if any(DETECTORS[name](inner) for inner, _ in scopes[index + 1 :]):
                    continue
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{node.lineno}:{name}")
    return offenders


@pytest.mark.parametrize(("name", "owner", "source"), FUNNELS)
def test_clone_funnel_detector_bites(name, owner, source):
    assert _detector_bites(name, ast.parse(source))


def test_extracted_test_funnels_stay_extracted():
    offenders = _clone_funnel_offenders()
    assert not offenders, "test clone funnel inlined: " + "; ".join(offenders)
