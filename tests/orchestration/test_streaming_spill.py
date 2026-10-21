"""Streaming spill: page results serialize to disk as each batch completes.

Peak memory scales with batch size, not total pages; ``crawl_request`` writes
one JSON line per page in append order, and temp spills are cleaned up.
"""

import json
import os
import tempfile

from tests._fetcher_factories import crawled_results


def _pages():
    return {
        "/": (
            200,
            '<html><body><a href="/a">a</a><a href="https://instagram.com/city">ig</a></body></html>',
            "text/html",
        ),
        "/a": (200, "<html>aaa</html>", "text/html"),
    }


def _policy(scope=True):
    """The policy half of the crawl, as (limits, policy) for crawled_results.

    Split so the spill-path variants below are a chokepoint call with one more
    argument rather than a second hand-built payload.
    """
    policy: dict = {}
    if scope:
        # keep the crawl on the seed host so instagram stays recorded-but-unfetched
        policy["scope_regex"] = r"^https?://example\.com(/|$)"
    return {"max_depth": 1, "max_pages": 10}, policy


async def _crawl(*, scope=True, **kwargs):
    limits, policy = _policy(scope)
    return await crawled_results(_pages(), limits=limits, policy=policy, **kwargs)


async def test_spill_file_gets_one_json_line_per_page(tmp_path):
    sp = tmp_path / "spill.ndjson"
    out = await _crawl(spill_path=str(sp))
    lines = [json.loads(line) for line in sp.read_text().splitlines() if line.strip()]
    assert len(lines) == len(out["results"]) == 2
    assert sorted(entry["i"] for entry in lines) == [0, 1]
    for entry in lines:
        assert isinstance(entry["r"], dict)
        assert "normalized_url" in entry["r"]


async def test_response_equivalent_with_and_without_spill(tmp_path):
    straight = await _crawl()
    spilled = await _crawl(spill_path=str(tmp_path / "s.ndjson"))
    assert straight["results"] == spilled["results"]
    assert straight["submitted_count"] == spilled["submitted_count"]


async def test_auto_spill_deleted_after_successful_request(monkeypatch):
    import app.orchestration.engine as engine

    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        if kwargs.get("prefix", args[0] if args else "").startswith(engine.SPILL_PREFIX):
            created.append(path)
        return fd, path

    monkeypatch.setattr(engine.tempfile, "mkstemp", spy_mkstemp)
    out = await _crawl()
    assert out["results"], "sanity: crawl produced results"
    assert created, "engine did not create an auto spill file"
    assert all(not os.path.exists(p) for p in created)


async def test_stale_spills_garbage_collected_on_next_crawl(monkeypatch):
    import app.orchestration.engine as engine

    # The cadence gate skips a temp-dir sweep inside the interval; reset the stamp so this crawl is the one
    # that performs the sweep the test exists to observe.
    monkeypatch.setattr(engine, "spill_gc_last_run", None)

    # The names are per-process: the sweep looks in the shared system temp
    # directory, so a fixed name would let one process delete another's file.
    tag = f"{os.getpid()}"
    stale = os.path.join(tempfile.gettempdir(), engine.SPILL_PREFIX + f"stale-{tag}.ndjson")
    fresh = os.path.join(tempfile.gettempdir(), engine.SPILL_PREFIX + f"fresh-{tag}.ndjson")
    for path in (stale, fresh):
        with open(path, "w") as fh:
            fh.write("{}\n")
    old = __import__("time").time() - 7200
    os.utime(stale, (old, old))

    try:
        await _crawl()
        assert not os.path.exists(stale), "stale spill survived GC"
        assert os.path.exists(fresh), "GC ate a fresh spill from another live request"
    finally:
        for path in (stale, fresh):
            if os.path.exists(path):
                os.unlink(path)
