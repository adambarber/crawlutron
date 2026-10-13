import ast

from tests.chokepoint_walks import local_nodes, nodes_of

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


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
        # Specialising the neutraliser is routing through it, not inlining it: the set of closers still came
        # from the chokepoint.
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
