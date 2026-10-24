"""Test helpers for the extraction pipeline."""

from __future__ import annotations

__all__ = ["format_extracted_document_text"]

from dataclasses import replace
from typing import TypedDict, Unpack

import app.extraction.content as _extraction_module
from app.config.settings import CrawlerSettings
from app.extraction.content import (
    detect_pagination as _production_detect_pagination,
)
from app.extraction.content import (
    extract_content as _production_extract_content,
)
from app.extraction.content import (
    extract_discovered_links as _production_extract_discovered_links,
)
from app.extraction.content import (
    extract_js_endpoints as _extract_js_endpoints_with_policy,
)
from app.extraction.content import (
    html_diagnostics as _production_html_diagnostics,
)
from app.extraction.document_text import format_extracted_document_text
from app.transport.fetchers import default_http_fetcher as _production_default_http_fetcher
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class _SettingsOverrides(TypedDict, total=False):
    """The subset of CrawlerSettings fields tests may override."""

    extraction_doc_timeout: float
    extraction_selector_timeout: float
    extraction_doc_acquire_timeout: float
    extraction_max_markdown: int


def html_diagnostics(html, source_url):
    return _production_html_diagnostics(html, source_url)


def extract_content(content, content_type, url, selectors=None):
    return _production_extract_content(content, content_type, url, selectors)


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


def settings_with(**overrides: Unpack[_SettingsOverrides]) -> CrawlerSettings:
    """Settings carrying the extraction deadlines a test wants, built from mapping
    and overridden, so a test that only cares about one deadline still runs against
    whatever the environment says about every other knob -- as production does.
    """
    return replace(CrawlerSettings.from_mapping(), **overrides)
