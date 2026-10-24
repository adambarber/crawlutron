from dataclasses import replace
from typing import Any

import app.extraction.content as _extraction_module
from app.config.settings import CrawlerSettings
from app.extraction.content import detect_pagination as _production_detect_pagination
from app.extraction.content import extract_content
from app.extraction.content import extract_discovered_links as _production_extract_discovered_links
from app.extraction.content import extract_js_endpoints as _extract_js_endpoints_with_policy
from app.extraction.content import html_diagnostics as _production_html_diagnostics
from app.extraction.document_text import format_extracted_document_text
from app.transport.document import format_table
from app.transport.fetchers import default_http_fetcher as _production_default_http_fetcher
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def html_diagnostics(html, source_url):
    return _production_html_diagnostics(html, source_url)


def extract_js_endpoints(endpoints, source_url=None, *, policy=UNRESTRICTED_HOST_POLICY):
    return _extract_js_endpoints_with_policy(endpoints, source_url, host_policy=policy)


def detect_pagination(html, final_url):
    return _production_detect_pagination(html, final_url)


def extract_discovered_links(
    html,
    source_url,
    *,
    max_links=250,
    tree=None,
):
    return _production_extract_discovered_links(
        html,
        source_url,
        max_links=max_links,
        tree=tree,
    )


def doc_worker_acquisitions(monkeypatch):
    """Record calls to the production worker-acquisition chokepoint, watching the one
    decision that can fork a markitdown worker; patching Process hid more than it
    showed, and the spy answers ``None`` so a slip degrades instead of raising.
    """
    acquisitions: list[bool] = []

    def spy(timeout) -> None:
        acquisitions.append(True)
        return None

    monkeypatch.setattr(_extraction_module.DOC_POOL, "acquire", spy)
    return acquisitions


async def default_http_fetcher(*args, **kwargs):
    return await _production_default_http_fetcher(*args, **kwargs)


__all__ = [
    "default_http_fetcher",
    "detect_pagination",
    "extract_discovered_links",
    "format_extracted_document_text",
    "format_table",
    "html_diagnostics",
    "doc_worker_acquisitions",
    "extract_content",
    "extract_js_endpoints",
]


def settings_with(**overrides: Any) -> CrawlerSettings:
    """Settings carrying the extraction deadlines a test wants, built from mapping
    and overridden, so a test that only cares about one deadline still runs against
    whatever the environment says about every other knob -- as production does.
    """
    return replace(CrawlerSettings.from_mapping(), **overrides)
