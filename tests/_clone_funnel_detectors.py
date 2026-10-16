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

    Copying the seeder + runner pair inlines the recovery wiring again; a call
    to the funnel itself is sanctioned and must not be reported."""
    return _has_call(node, "_seed_job") and _has_call(node, "run_seeded_job")


def _blocking_route(node):
    return _has_call(node, "get_ident") and _has_call(node, "sleep")


def _response_handler(node):
    return _has_call(node, "_make_response_handler") and any(
        isinstance(child, ast.ClassDef) for child in nodes_of(node)
    )


def _http_strategy(node):
    """A call re-spelling one page's identity inline instead of the record.

    When the identity fields moved into ``StrategyRequest``, the match had to
    follow them into the ``request=`` keyword or it could never fire again."""
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

    Matched on the arithmetic, not a single literal, so the detector survives a
    reformat and still fires on a re-derived constant."""
    dumped = ast.dump(node)
    return "50" in dumped and "1024" in dumped


def _scripted_fetcher(node):
    """A def re-declaring the production ``http_fetcher`` contract.

    Hard-coding the 50 MiB ceiling asserts it by copying it, and a copy still
    passes when production lowers it; the ``headers`` guard shapes the match."""
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
    """A test building a private-resolution precondition by hand.

    Both halves are required: the escape hatch closed AND the resolver pinned;
    a copy missing one half fails or passes for the wrong reason."""
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

    Reading ``["results"]`` off the response is the assertion that the request
    ran synchronously; status/headers/422 tests legitimately need the raw body."""
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

    The pool's contract is signal semantics, so a stand-in is any class with
    is_alive/terminate/kill/join; ``_process_doubles.StubProc`` models it once."""
    if not isinstance(node, ast.ClassDef):
        return False
    return _PROCESS_DOUBLE_METHODS <= {
        child.name for child in node.body if isinstance(child, ast.FunctionDef)
    }


def _lifespan_teardown(node):
    """A hand-built "neutralise the heavy lifespan teardown" setup.

    Re-spelling a subset of production's closer list reaches the real teardown
    for whatever the test forgot; ``stub_heavy_teardown`` derives the set."""
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
    """A test crawling scripted pages by hand instead of through crawled_urls.

    A ``crawl_request`` with a fresh ``site_fetcher(...)`` is exactly what
    ``crawled_urls`` does; a copied call can drift from the chokepoint."""
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
