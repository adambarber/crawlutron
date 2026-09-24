from _policy_factories import UNRESTRICTED_HOST_POLICY

from app.crawl.document_text import (
    _collect_document_item_lines,
    _document_item_range,
    _format_extracted_document_text,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _looks_like_document_heading,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from app.crawl.extraction import (
    _detect_pagination as _detect_pagination_with_policy,
)
from app.crawl.extraction import (
    _extract_discovered_links as _extract_discovered_links_with_policy,
)
from app.crawl.extraction import (
    _html_diagnostics as _html_diagnostics_with_policy,
)
from app.crawl.extraction import extract_content as _extract_content_with_policy
from app.crawl.extraction import extract_js_endpoints as _extract_js_endpoints_with_policy
from app.crawl.strategies.document import (
    _default_document_text_extractor as _default_document_text_extractor_with_policy,
)
from app.crawl.strategies.document import _format_table
from app.crawl.strategies.fetchers import _default_http_fetcher as _default_http_fetcher_with_policy


def _html_diagnostics(html, source_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _html_diagnostics_with_policy(html, source_url, host_policy=policy)


def extract_content(html, content_type, source_url, css_selector=None, xpath=None):
    return _extract_content_with_policy(html, content_type, source_url, css_selector, xpath)


def extract_js_endpoints(endpoints, source_url=None, *, policy=UNRESTRICTED_HOST_POLICY):
    return _extract_js_endpoints_with_policy(endpoints, source_url, host_policy=policy)


def _detect_pagination(html, final_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _detect_pagination_with_policy(html, final_url, host_policy=policy)


def _extract_discovered_links(
    html,
    source_url,
    *,
    host_policy=UNRESTRICTED_HOST_POLICY,
    max_links=250,
    tree=None,
):
    return _extract_discovered_links_with_policy(
        html,
        source_url,
        host_policy=host_policy,
        max_links=max_links,
        tree=tree,
    )


def _default_document_text_extractor(url, policy=UNRESTRICTED_HOST_POLICY):
    return _default_document_text_extractor_with_policy(url, policy)


async def _default_http_fetcher(*args, policy=UNRESTRICTED_HOST_POLICY, **kwargs):
    return await _default_http_fetcher_with_policy(*args, policy=policy, **kwargs)


__all__ = [
    "_collect_document_item_lines",
    "_default_document_text_extractor",
    "_default_http_fetcher",
    "_detect_pagination",
    "_document_item_range",
    "_extract_discovered_links",
    "_format_extracted_document_text",
    "_format_table",
    "_html_diagnostics",
    "_is_document_item_marker",
    "_is_document_non_item_section",
    "_join_document_paragraph_lines",
    "_looks_like_document_heading",
    "_next_non_empty_document_line",
    "_should_join_document_paragraph",
    "extract_content",
    "extract_js_endpoints",
]
