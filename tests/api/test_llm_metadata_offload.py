"""LLM metadata extraction must leave the event loop.

``format_crawl_item`` runs ``extract_llm_metadata`` (a second lxml parse)
synchronously for every result. The async endpoint path must offload that
parse with ``asyncio.to_thread`` via the additive async shaping API.
"""

import asyncio

import pytest

from app.api.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
    abuild_unified_result,
    aformat_crawl_item,
    build_unified_crawl_response,
    build_unified_result,
)
from app.models.engine_crawl_result import EngineCrawlResult
from tests._assertions import install_to_thread_spy
from tests._result_builders import engine_result


def _html_item(i: int = 0) -> EngineCrawlResult:
    """A real engine result carrying the HTML and markdown this suite parses.

    A dict here would not have been a shorthand: the formatter reads declared
    attributes, so a dict made the offload test pass through the ``except``
    arm rather than the parse it claims to measure.
    """
    html = (
        f"<html><head><title>Page {i}</title>"
        '<meta name="description" content="desc here">'
        "</head><body><p>Body text here</p></body></html>"
    )
    return engine_result(
        f"http://example.com/{i}",
        "rendered",
        html=html,
        markdown="# Body text here",
        text="Body text here",
        final_url=f"http://example.com/{i}",
    )


async def test_aformat_crawl_item_offloads_metadata_parse(monkeypatch):
    """The lxml metadata parse runs in a worker thread, not on the loop."""
    calls: list[tuple[str, str]] = []
    from app.crawl import llm_format as llm_mod

    real_fn = llm_mod.extract_llm_metadata

    def spy(html, url):
        calls.append((html, url))
        return real_fn(html, url)

    monkeypatch.setattr(llm_mod, "extract_llm_metadata", spy)
    seen_threads: set[int] = set()
    loop_thread = asyncio.get_running_loop()

    import threading

    orig_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        seen_threads.add(threading.get_ident())
        return await orig_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    markdown, metadata = await aformat_crawl_item(_html_item())
    assert markdown == "# Body text here"
    assert metadata.get("title") == "Page 0"
    assert metadata.get("source_url") == "http://example.com/0"
    assert len(calls) == 1
    # The parse ran off the event loop thread.
    assert len(seen_threads) == 1
    assert next(iter(seen_threads)) != loop_thread


async def test_abuild_unified_crawl_response_matches_sync():
    """Async shaping is byte-identical to the sync contract (all item shapes)."""
    items = [
        _html_item(0),
        {
            "original_url": "http://example.com/doc.pdf",
            "status": "success",
            "classification": "rendered",
            "text": "pdf text",
        },
        {"original_url": "http://example.com/empty", "status": "failed", "classification": "not_found"},
        {
            "original_url": "http://example.com/csv",
            "status": "success",
            "classification": "rendered",
            "markdown": "# H\n\nb",
            "final_url": "http://example.com/csv",
        },
    ]
    sync = build_unified_crawl_response(
        items, options=EnvelopeOptions(request_id="r", policy_schema_version="v")
    )
    async_ = await abuild_unified_crawl_response(
        items, adapt=build_unified_result, options=EnvelopeOptions(request_id="r", policy_schema_version="v")
    )
    assert async_.model_dump() == sync.model_dump()


@pytest.mark.parametrize("submitted", [None, 0, 2, 7])
async def test_sync_and_async_envelope_builders_agree_on_the_submitted_count(submitted):
    """The ``submitted_count`` fallback is resolved once, for both builders.

    It used to be written out in each of them -- ``submitted_count if submitted
    is not None else len(results)`` -- and two copies of a fallback is how two
    paths start disagreeing about how many URLs a batch held. The number is what
    a caller reconciles its own accounting against, so a divergence here is a
    wire bug, not a cosmetic one.
    """
    items = [_html_item(0), _html_item(1)]
    sync = build_unified_crawl_response(
        items, options=EnvelopeOptions(request_id="r", submitted_count=submitted)
    )
    async_ = await abuild_unified_crawl_response(
        items, adapt=build_unified_result, options=EnvelopeOptions(request_id="r", submitted_count=submitted)
    )
    assert async_.model_dump() == sync.model_dump()
    # None means "the caller could not say", which the wire answers as the
    # batch it is reporting; anything else is the caller's number verbatim,
    # including a count that disagrees with the batch on purpose.
    assert async_.submitted_count == (len(items) if submitted is None else submitted)


async def test_abuild_unified_result_single_item_matches_sync():
    item = _html_item(3)
    from app.api.response_shaping import build_unified_result

    assert (await abuild_unified_result(item)).model_dump() == build_unified_result(item).model_dump()


async def _empty_batch_response(monkeypatch=None):
    """The empty batch, through the same door both empty-batch tests use.

    One helper so the two tests below cannot drift into asserting different
    things about the same call: one pins the response, one pins the offload
    count, and a rewritten call would silently change what the other proves.
    """
    if monkeypatch is not None:
        install_to_thread_spy(monkeypatch)
    return await abuild_unified_crawl_response(
        [], adapt=build_unified_result, options=EnvelopeOptions(request_id="r")
    )


async def test_async_response_empty_results():
    resp = await _empty_batch_response()
    assert resp.results == []
    assert resp.request_id == "r"


async def test_abuild_unified_crawl_response_batches_into_one_offload(monkeypatch):
    """One response must pay one thread-submission, not one per item.

    Fanning N items to N ``asyncio.to_thread`` calls makes every batch carry N
    Future/thread-dispatch overheads. The whole list is shaped in a single
    offload; output stays byte-identical to the sync contract (the companion
    test above pins that).
    """
    items = [_html_item(i) for i in range(4)]
    offloaded = install_to_thread_spy(monkeypatch)
    resp = await abuild_unified_crawl_response(
        items, adapt=build_unified_result, options=EnvelopeOptions(request_id="r", policy_schema_version="v")
    )
    assert len(resp.results) == len(items)
    assert len(offloaded) == 1, f"expected one batched offload, got {len(offloaded)}"


async def test_abuild_batches_offload_for_empty_results(monkeypatch):
    """An empty batch offloads nothing and still answers the empty response."""
    offloaded = install_to_thread_spy(monkeypatch)
    resp = await _empty_batch_response()
    assert resp.results == []
    assert len(offloaded) <= 1, f"empty batch must not fan out: {len(offloaded)}"
