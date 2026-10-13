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
FUNNEL_HOME_MODULES = frozenset(
    {
        "_fetcher_factories.py",
        "_fetch_doubles.py",
        "_fetch_scripted.py",
        "_crawl_runners.py",
        # The fetchers, the crawls and the doubles are declared once, in the modules that own them. A copy in
        # any other module re-asserts the contract, and that is the drift these detectors exist to catch.
        "_assertions.py",
        "_process_doubles.py",
        "_lifespan_doubles.py",
    }
)
