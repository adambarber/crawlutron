import ast

import pytest
from chokepoint_walks import TESTS_DIR, trees_under

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

FUNNELS = [
    ("webhook-delivery", "poll_webhook_delivery", 'def f(records):\n return poll_until(done, json.loads(records[0].decode("utf-8")))'),
    ("async-submit", "submit_async_crawl", 'async def f(client):\n await client.post("/v1/crawl", json={})\n await poll_until(done)'),
    ("render-pool", "post_render_with_pool", 'async def f(client, pool):\n set_browser_pool(pool)\n await client.post("/v1/render", json={})'),
    ("job-post", "post_api_job", 'async def f(client):\n await client.post("/v1/jobs", json={"root_url": "x", "policy": _execution_policy(None)})'),
    ("seeded-scripted-job", "run_seeded_scripted_job", 'async def f():\n job = _seed_job(store, url, policy)\n return await _run_scripted_persisted_job(job, store, results)'),
    ("blocking-route", "blocking_pinned_request", 'def build():\n import threading\n import time\n threading.get_ident()\n time.sleep(0.05)'),
    ("response-handler", "response_handler_double", 'async def f(collected):\n handler = BrowserRenderer._make_response_handler(collected)\n class R:\n  async def json(self): pass\n await handler(R())'),
    ("http-strategy", "run_http_strategy", 'async def f():\n return await _http_strategy_result(original_url=url, normalized_url=url, strategy="HTTP_DECLARED_CRAWLER", policy=policy, http_fetcher=fetcher, host_policy=HostPolicy.unrestricted())'),
    ("cache-entries", "make_expiring_cache", 'def f(tmp):\n cache = ContentCache(tmp, default_ttl=10, clock=clock, max_entries=100, max_total_bytes=10000)\n cache.set("a", "x" * 100)\n cache.set("b", "x" * 100)'),
    ("html-fetcher", "make_html_fetcher", 'def f(calls):\n async def fetcher(url, headers, timeout):\n  calls.append(url)\n  return make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=body)'),
    ("loop-rebind", "run_loop_rebind_scenario", 'async def f(get):\n first = get()\n foreign = asyncio.new_event_loop()\n first._bound_loop = foreign\n await asyncio.sleep(0)\n foreign.close()'),
    ("default-fetcher", "run_default_http_fetcher", 'async def f():\n return await _default_http_fetcher(url, {}, 30, policy=HostPolicy.unrestricted())'),
    ("job-store", "make_job_store", 'def f(tmp):\n return JobStore(tmp / "jobs.db", policy=UNRESTRICTED_HOST_POLICY)'),
]


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
        for child in ast.walk(node)
    )


def _literal(node):
    return node.value if isinstance(node, ast.Constant) else None


def _has_attr_assign(node, name):
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == name for target in child.targets)
        for child in ast.walk(node)
    )


def _webhook_delivery(node):
    return _has_call(node, "poll_until") and _has_call(node, "loads") and _has_call(node, "decode")


def _async_submit(node):
    return _has_call(node, "post", "/v1/crawl") and _has_call(node, "poll_until")


def _render_pool(node):
    return _has_call(node, "set_browser_pool") and _has_call(node, "post", "/v1/render")


def _job_post(node):
    return _has_call(node, "post", "/v1/jobs") and _has_call(node, "_execution_policy")


def _seeded_scripted_job(node):
    return _has_call(node, "_seed_job") and _has_call(node, "_run_scripted_persisted_job")


def _blocking_route(node):
    return _has_call(node, "get_ident") and _has_call(node, "sleep")


def _response_handler(node):
    return _has_call(node, "_make_response_handler") and any(
        isinstance(child, ast.ClassDef) for child in ast.walk(node)
    )


def _http_strategy(node):
    required = {"original_url", "normalized_url", "strategy", "policy", "http_fetcher", "host_policy"}
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or _name(child.func) != "_http_strategy_result":
            continue
        words = {keyword.arg: keyword.value for keyword in child.keywords}
        if (
            required <= words.keys()
            and _literal(words["strategy"]) == "HTTP_DECLARED_CRAWLER"
            and ast.dump(words["original_url"]) == ast.dump(words["normalized_url"])
        ):
            return True
    return False


def _cache_entries(node):
    values = [
        child.args[1]
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _name(child.func) == "set" and len(child.args) > 1
    ]
    hundred_byte_values = sum(
        isinstance(value, ast.BinOp)
        and isinstance(value.op, ast.Mult)
        and _literal(value.right) == 100
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
        for child in ast.walk(node)
    )
    exact_fetch = any(
        isinstance(child, ast.Call)
        and _name(child.func) == "make_fetch"
        and any(keyword.arg == "status" and _literal(keyword.value) == 200 for keyword in child.keywords)
        and any(
            keyword.arg == "ctype" and _literal(keyword.value) == "text/html"
            for keyword in child.keywords
        )
        and any(
            keyword.arg == "body" and isinstance(keyword.value, ast.Name) and keyword.value.id == "body"
            for keyword in child.keywords
        )
        for child in ast.walk(node)
    )
    return appends_url and exact_fetch


def _loop_rebind(node):
    return _has_call(node, "new_event_loop") and _has_attr_assign(node, "_bound_loop")


def _default_fetcher(node):
    return any(
        isinstance(child, ast.Call)
        and _name(child.func) == "_default_http_fetcher"
        and len(child.args) == 3
        and isinstance(child.args[1], ast.Dict)
        and _literal(child.args[2]) == 30
        and any(
            keyword.arg == "policy"
            and isinstance(keyword.value, ast.Call)
            and _name(keyword.value.func) == "unrestricted"
            for keyword in child.keywords
        )
        for child in ast.walk(node)
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
        for child in ast.walk(node)
    )


DETECTORS = {
    "webhook-delivery": _webhook_delivery,
    "async-submit": _async_submit,
    "render-pool": _render_pool,
    "job-post": _job_post,
    "seeded-scripted-job": _seeded_scripted_job,
    "blocking-route": _blocking_route,
    "response-handler": _response_handler,
    "http-strategy": _http_strategy,
    "cache-entries": _cache_entries,
    "html-fetcher": _html_fetcher,
    "loop-rebind": _loop_rebind,
    "default-fetcher": _default_fetcher,
    "job-store": _job_store,
}


def _scopes(nodes):
    for node in nodes:
        if isinstance(node, FUNCTION_TYPES):
            yield node
        else:
            yield from _scopes(ast.iter_child_nodes(node))


def _detector_bites(name, tree):
    return any(DETECTORS[name](node) for node in _scopes(tree.body))


def _clone_funnel_offenders():
    offenders = []
    for path, tree in trees_under(TESTS_DIR):
        for node in _scopes(tree.body):
            for name, owner, _source in FUNNELS:
                if node.name != owner and DETECTORS[name](node):
                    offenders.append(f"{path.relative_to(TESTS_DIR)}:{node.lineno}:{name}")
    return offenders


@pytest.mark.parametrize(("name", "owner", "source"), FUNNELS)
def test_clone_funnel_detector_bites(name, owner, source):
    assert _detector_bites(name, ast.parse(source))


def test_extracted_test_funnels_stay_extracted():
    offenders = _clone_funnel_offenders()
    assert not offenders, "test clone funnel inlined: " + "; ".join(offenders)
