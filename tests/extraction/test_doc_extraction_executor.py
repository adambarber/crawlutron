"""Document extraction must not occupy the shared asyncio default executor.

The document branch blocks on ``DOC_POOL.acquire`` up to the acquire timeout, so
it must run on its own pool, never crowding the store control plane's threads.
"""

import threading
from typing import cast

from app.extraction import documents as docex
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.transport import content_pipeline as pipeline
from app.vocabulary.extraction import EXTRACTION_DOCUMENT
from tests._policy_factories import http_strategy_policy

_DOC_EXECUTOR_PREFIX = "doc-extract"


def _thread_recording_document(seen: dict[str, threading.Thread]):
    """A stand-in for the real document extractor that records the thread it ran on."""

    def spy(payload, url, settings=None):
        seen["thread"] = threading.current_thread()
        return pipeline.ExtractedContent(text="", markdown=None, extraction_method="")

    return spy


async def test_document_extraction_runs_on_dedicated_executor_not_default(monkeypatch):
    """The document branch runs on the dedicated doc pool, not the default executor."""
    seen: dict[str, threading.Thread] = {}
    monkeypatch.setattr(docex, "extract_document", _thread_recording_document(seen))

    result = await pipeline.run_extraction(
        extraction=cast("CrawlExtractionMethod", EXTRACTION_DOCUMENT),
        content=b"some bytes",
        decoded="some bytes",
        content_type=None,
        final_url=None,
        policy=http_strategy_policy(extraction=EXTRACTION_DOCUMENT),
        default_method="document_extract",
        html=False,
    )

    assert seen, "the document extraction was never dispatched"
    thread_name = seen["thread"].name
    assert thread_name.startswith(_DOC_EXECUTOR_PREFIX), (
        f"document extraction ran on {thread_name!r}; it must use a dedicated "
        f"executor named {_DOC_EXECUTOR_PREFIX!r} so a pool-wait never occupies "
        "the shared default executor the store control plane needs"
    )
    # The wait blocking on the pool must not be what the pipeline reports back.
    assert result is not None
