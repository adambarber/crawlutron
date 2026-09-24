"""Browser strategy result composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.constants import (
    DEFAULT_EXTRACTION,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
)
from app.crawl.extraction import (
    _extract_document,
    _extract_html,
    extract_content,
    extract_post_fetch_discovery,
)
from app.crawl.render_options import resolve_render_options
from app.crawl.result_helpers import _make_engine_result
from app.crawl.strategies.body_decode import _quality_score
from app.crawl.worker import call_worker
from app.models import (
    CrawlJob,
    CrawlStrategyAttempt,
    CrawlStrategyName,
    CrawlWorker,
    EngineCrawlResult,
    RenderOptions,
)
from app.validation import HostPolicy, filter_js_endpoints, registrable_domain


@dataclass(frozen=True)
class _BrowserContent:
    text: str
    markdown: str | None
    extraction_method: str


def _browser_options(policy: dict[str, Any]) -> RenderOptions:
    return resolve_render_options(
        None,
        {
            "timeout_seconds": policy["limits"]["timeout_seconds"],
            "max_retries": policy["limits"]["max_retries"],
            "respect_robots": True,
            "include_html": True,
            "include_text": True,
            "browser_profile": policy.get("browser_profile") or {},
        },
    )


def _browser_job(normalized_url: str, options: RenderOptions, host_policy: HostPolicy) -> CrawlJob:
    return CrawlJob(
        index=0,
        url=normalized_url,
        domain=registrable_domain(normalized_url),
        options=options,
        host_policy=host_policy,
    )


async def _extract_browser_content(
    content: str | bytes | bytearray | None,
    result: Any,
    policy: dict[str, Any],
) -> _BrowserContent:
    if not content:
        return _BrowserContent(
            text="",
            markdown=None,
            extraction_method=result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT,
        )
    extraction = policy.get("extraction", DEFAULT_EXTRACTION)
    if extraction == EXTRACTION_DOCUMENT:
        extracted = await asyncio.to_thread(
            _extract_document,
            content.encode("utf-8", "replace") if isinstance(content, str) else content,
            result.final_url,
        )
        text = extracted.text or ""
        markdown = extracted.markdown
        method = EXTRACTION_METHOD_DOCUMENT
    elif extraction == EXTRACTION_TRAFILATURA:
        extracted = await asyncio.to_thread(
            _extract_html,
            content,
            result.final_url,
            css_selector=policy.get("css_selector"),
            xpath=policy.get("xpath"),
        )
        text = extracted.text or ""
        markdown = extracted.markdown
        method = EXTRACTION_METHOD_TRAFILATURA
    else:
        extracted = await asyncio.to_thread(
            extract_content,
            content,
            None,
            result.final_url,
            css_selector=policy.get("css_selector"),
            xpath=policy.get("xpath"),
        )
        text = extracted.text or ""
        markdown = extracted.markdown
        method = result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT
    return _BrowserContent(text=text, markdown=markdown, extraction_method=method)


async def _discover_browser_content(
    result: Any,
    policy: dict[str, Any],
    host_policy: HostPolicy,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        extract_post_fetch_discovery,
        result.html,
        result.final_url,
        js_base_url=result.final_url,
        max_links=policy["limits"]["max_discovered_links"],
        include_diagnostics=True,
        include_links=result.status == "success",
        include_js_endpoints=True,
        include_metadata=True,
        host_policy=host_policy,
    )


def _browser_diagnostics(
    result: Any,
    content: _BrowserContent,
    policy: dict[str, Any],
    host_policy: HostPolicy,
    post_fetch: dict[str, Any],
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "browser_profile": policy.get("browser_profile") or {},
        "runtime": {"elapsed_ms": result.elapsed_ms, "final_url": result.final_url},
        "content_metrics": {
            "visible_text_length": len(content.text.strip()),
            "html_length": len((result.html or "").strip()),
        },
        **post_fetch["diagnostics"],
    }
    for key in ("intercepted_xhr", "screenshot"):
        value = result.metadata.get(key)
        if value:
            diagnostics[key] = value
    if post_fetch["next_page_url"]:
        diagnostics["next_page_url"] = post_fetch["next_page_url"]
    diagnostics["js_endpoints"] = filter_js_endpoints(
        post_fetch["js_endpoints"],
        resolve_against=result.final_url,
        policy=host_policy,
    )
    return diagnostics


def _build_browser_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName,
    result: Any,
    content: _BrowserContent,
    diagnostics: dict[str, Any],
    post_fetch: dict[str, Any],
) -> EngineCrawlResult:
    attempt = CrawlStrategyAttempt(
        strategy=strategy,
        status=result.status,
        classification=result.classification,
        http_status=None,
        content_type=None,
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=result.final_url,
        status=result.status,
        classification=result.classification,
        selected_strategy=strategy if result.status == "success" else None,
        attempt=attempt,
        http_status=None,
        headers={},
        content_type=None,
        html=result.html,
        text=content.text,
        markdown=content.markdown,
        extraction_method=content.extraction_method,
        quality_score=_quality_score(
            result.status, result.classification, content.text, result.html, None
        ),
        classification_evidence=[],
        diagnostics=diagnostics,
        discovered_link_count=post_fetch["discovered_link_count"],
        discovered_links_truncated=post_fetch["discovered_links_truncated"],
        discovered_links=post_fetch["discovered_links"],
        error=result.error,
        next_page_url=post_fetch["next_page_url"],
        validators={},
        llm_metadata=post_fetch["llm_metadata"] or None,
    )


async def browser_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName,
    policy: dict[str, Any],
    browser_worker: CrawlWorker,
    host_policy: HostPolicy,
) -> EngineCrawlResult:
    result = await call_worker(browser_worker, _browser_job(normalized_url, _browser_options(policy), host_policy))
    content = result.html if result.html else result.text
    extracted = await _extract_browser_content(content, result, policy)
    post_fetch = await _discover_browser_content(result, policy, host_policy)
    diagnostics = _browser_diagnostics(result, extracted, policy, host_policy, post_fetch)
    return _build_browser_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        result=result,
        content=extracted,
        diagnostics=diagnostics,
        post_fetch=post_fetch,
    )


__all__ = ["browser_strategy_result"]
