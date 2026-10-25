"""robots.txt gathering must run off the event loop (concurrent, not sequential).

The engine awaits ``robots_allows``/``robots_crawl_delay`` one URL at a time,
so N URLs paid N sequential round-trips; tests pin the concurrent contract.
"""

import asyncio
from contextlib import asynccontextmanager

from app.admission.admission import RobotsGate
from app.models.crawl_policy import CrawlPolicy
from app.models.discovered_link import DiscoveredLink
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._recursive_harnesses import drive_recursive_crawl
from tests._recursive_harnesses import make_crawl_task as _make_job
from tests._result_builders import make_engine_result


@asynccontextmanager
async def _overlap_window(state: dict[str, object]):
    """Hold ``state``'s in-flight window across one scheduling point.

    The guarded call yields once, so every concurrent caller is inside the
    window; written once because the increment/decrement pair gets copied wrong.
    """
    state["in_flight"] += 1
    state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
    try:
        yield
        await asyncio.sleep(0)
    finally:
        state["in_flight"] -= 1


def _tracker():
    """Robots closure pair tracking max in-flight overlap.

    ``first_screen_max`` narrows the window to the first screening of each URL,
    since a single all-calls max would not pin the seam a test is about.
    """
    state = {"in_flight": 0, "max_in_flight": 0, "first_screen_max": 0, "calls": 0}
    screened: set[str] = set()

    async def _guarded(url: str) -> None:
        first_screening = url not in screened
        screened.add(url)
        async with _overlap_window(state):
            if first_screening:
                state["first_screen_max"] = max(state["first_screen_max"], state["in_flight"])

    async def allows(url):
        state["calls"] += 1
        await _guarded(url)
        return True

    async def crawl_delay(url):
        await _guarded(url)
        return None

    return allows, crawl_delay, state


async def _drive(jobs, worker, allows, crawl_delay, **kwargs):
    run = await drive_recursive_crawl(jobs, worker, allows=allows, crawl_delay=crawl_delay, **kwargs)
    return run.spill


async def test_recursive_batch_robots_checks_run_concurrently():
    """Five seed URLs must be robots-screened with overlap, not one-by-one."""
    allows, crawl_delay, state = _tracker()
    jobs = [_make_job(i, f"host{i}.example") for i in range(5)]

    async def worker(job):
        return make_engine_result(job.normalized_url)

    await _drive(jobs, worker, allows, crawl_delay)
    assert state["calls"] == 5
    assert state["max_in_flight"] == 5
    assert state["first_screen_max"] == 5, (
        "the seed batch's first robots screening was not concurrent: the batch "
        f"screen seam is serialized (first-screen overlap {state['first_screen_max']})"
    )


async def test_recursive_link_robots_checks_run_concurrently():
    """Discovered links of one page must be robots-screened with overlap."""
    allows, crawl_delay, state = _tracker()
    links = [
        DiscoveredLink(
            url=f"http://link{i}.example/",
            normalized_url=canonical(f"http://link{i}.example/"),
            text="",
            rel=[],
            source_url=canonical("http://seed.example/"),
            host=f"link{i}.example",
            is_same_host=False,
        )
        for i in range(5)
    ]
    jobs = [_make_job(0, "seed.example")]

    async def worker(job):
        if job.normalized_url == canonical("http://seed.example/page0"):
            return make_engine_result(job.normalized_url, links=links)
        return make_engine_result(job.normalized_url)

    await _drive(jobs, worker, allows, crawl_delay, max_depth=1)
    assert state["calls"] >= 6  # seed batch screen + 5 discovered links
    assert state["max_in_flight"] == 5
    # Seam-specific: the discovered-link screen (admission) must be the one doing the overlapping work
    assert state["first_screen_max"] == 5, (
        "the discovered links of one page were not robots-screened with overlap: "
        f"the admission screen ran them one at a time (first-screen overlap "
        f"{state['first_screen_max']})"
    )


async def test_one_raising_robots_check_does_not_kill_the_batch():
    """A robots check that raises for one URL must not abort the whole crawl.

    Per-URL isolation: a single raising ``robots_allows`` fails closed to
    disallow that URL and the rest of the batch still admits and crawls.
    """

    async def allows(url):
        if url.raw == "http://poison.example/page0":
            raise RuntimeError("robots fetch exploded")
        return True

    jobs = [_make_job(0, "poison.example"), _make_job(0, "ok0.example"), _make_job(1, "ok1.example")]

    async def worker(job):
        return make_engine_result(job.normalized_url)

    run = await drive_recursive_crawl(jobs, worker, allows=allows)
    written = [run.spill.writes[i].normalized_url for i in run.spill.writes]
    assert canonical("http://poison.example/page0") not in written  # failed closed
    assert set(written) == {
        canonical("http://ok0.example/page0"),
        canonical("http://ok1.example/page1"),
    }


async def test_nonrecursive_robots_checks_run_concurrently(monkeypatch):
    """The engine's non-recursive branch must screen its batch with overlap."""
    from app.orchestration.engine import crawl_request

    state = {"in_flight": 0, "max_in_flight": 0}

    async def fake_fetcher(url, headers, timeout_seconds, max_content_length=0):
        if url.raw.endswith("/robots.txt"):
            async with _overlap_window(state):
                pass

            class _Resp:
                status_code = 200
                headers = {}
                body = b"User-agent: *\nDisallow:\n"
                final_url = url
                elapsed_ms = 1

            return _Resp()

        # A second name from the robots class above: they are two branches of one responder, and one name for
        # both meant the second definition shadowed the first for anything that read it.
        class _PageResp:
            status_code = 200
            headers = {"content-type": "text/html"}
            body = b"<html><body>hello world, enough text to render</body></html>"
            final_url = url
            elapsed_ms = 1

        return _PageResp()

    urls = [f"http://nc{i}.example/page" for i in range(5)]
    result = await crawl_request(
        {"urls": urls},
        http_fetcher=fake_fetcher,
        cffi_fetcher=fake_fetcher,
        browser_worker=None,
    )
    assert len(result["results"]) == 5
    assert state["max_in_flight"] == 5


async def test_gather_preserves_order_and_skips_delay_when_disallowed():
    """Order is input order; disallowed URLs never pay for a crawl-delay fetch."""
    from app.admission.admission_links import gather_robots_decisions

    delay_calls: list[str] = []

    async def allows(url):
        return url.raw != "http://b.example/"

    async def crawl_delay(url):
        delay_calls.append(url.raw)
        return 1.5

    out = await gather_robots_decisions(
        [canonical(text) for text in ("http://a.example/", "http://b.example/", "http://c.example/")],
        allows,
        crawl_delay,
    )
    assert out == [(True, 1.5), (False, None), (True, 1.5)]
    assert delay_calls == ["http://a.example/", "http://c.example/"]


async def test_recursive_screen_no_robots_calls_when_disrespected():
    """respect_robots=False must not touch the robots closures at all."""
    calls: list[str] = []

    async def allows(url):
        calls.append(url)
        return True

    jobs = [_make_job(i, f"nr{i}.example") for i in range(3)]

    async def worker(job):
        return make_engine_result(job.normalized_url)

    spill = (
        await drive_recursive_crawl(jobs, worker, allows=allows, crawl_delay=None, respect_robots=False)
    ).spill
    assert calls == []
    assert len(spill.writes) == 3


async def test_recursive_batch_duplicate_url_crawled_once(recording_worker):
    """A URL repeated inside one batch keeps the old visit-once semantics."""
    allows, crawl_delay, state = _tracker()
    jobs = [_make_job(0, "dup.example"), _make_job(0, "dup.example")]
    seen, worker = recording_worker

    await _drive(jobs, worker, allows, crawl_delay)
    assert seen == [canonical("http://dup.example/page0")]
    assert state["calls"] == 1


async def test_cold_robots_gate_coalesces_same_origin_fetches(monkeypatch):
    calls = 0

    async def fetch_robots(fetcher, url, headers, timeout_seconds):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "User-agent: *\nDisallow:\n", 200

    gate = RobotsGate(CrawlPolicy(), UNRESTRICTED_HOST_POLICY, fetch_robots)
    decisions = await asyncio.gather(
        *(gate.allows(canonical(f"https://example.test/page-{index}")) for index in range(5))
    )

    assert decisions == [True] * 5
    assert calls == 1
