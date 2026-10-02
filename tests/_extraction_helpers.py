from dataclasses import replace

import app.crawl.extraction as _extraction_module
from app.crawl.document_text import format_extracted_document_text
from app.crawl.extraction import (
    detect_pagination as _detect_pagination_with_policy,
)
from app.crawl.extraction import extract_content
from app.crawl.extraction import (
    extract_discovered_links as _extract_discovered_links_with_policy,
)
from app.crawl.extraction import extract_js_endpoints as _extract_js_endpoints_with_policy
from app.crawl.extraction import (
    html_diagnostics as _html_diagnostics_with_policy,
)
from app.crawl.strategies.document import (
    _default_document_text_extractor as _default_document_text_extractor_with_policy,
)
from app.crawl.strategies.document import _format_table
from app.crawl.strategies.fetchers import default_http_fetcher as _default_http_fetcher_with_policy
from app.settings import CrawlerSettings
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def html_diagnostics(html, source_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _html_diagnostics_with_policy(html, source_url, host_policy=policy)


def extract_js_endpoints(endpoints, source_url=None, *, policy=UNRESTRICTED_HOST_POLICY):
    return _extract_js_endpoints_with_policy(endpoints, source_url, host_policy=policy)


def detect_pagination(html, final_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _detect_pagination_with_policy(html, final_url, host_policy=policy)


def extract_discovered_links(
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


def doc_worker_acquisitions(monkeypatch):
    """Record every call to the production worker-acquisition chokepoint.

    ``DOC_POOL.acquire`` is the one production decision that can fork a
    markitdown worker, so a test that must prove a document was refused BEFORE
    any worker existed is watching exactly the right seam. Patching
    ``multiprocessing.Process`` instead proved the same thing the long way and
    hid more than it showed: a stand-in whose ``__init__`` swallowed every
    argument would keep passing if production changed how it constructs one, so
    the fake was a way of asserting something narrower than it looked.

    Returns the recording list, empty until production asks for a worker. The
    replacement answers ``None`` (no slot available), which is a state
    production already handles, so a call that slips through degrades the
    document instead of raising.
    """
    acquisitions: list[bool] = []

    def spy(timeout) -> None:
        acquisitions.append(True)
        return None

    monkeypatch.setattr(_extraction_module.DOC_POOL, "acquire", spy)
    return acquisitions


async def default_http_fetcher(*args, policy=UNRESTRICTED_HOST_POLICY, **kwargs):
    return await _default_http_fetcher_with_policy(*args, policy=policy, **kwargs)


__all__ = [
    "_default_document_text_extractor",
    "default_http_fetcher",
    "detect_pagination",
    "extract_discovered_links",
    "format_extracted_document_text",
    "_format_table",
    "html_diagnostics",
    "doc_worker_acquisitions",
    "extract_content",
    "extract_js_endpoints",
]


def settings_with(**overrides: object) -> CrawlerSettings:
    """Settings carrying the extraction deadlines a test wants.

    Built from ``from_mapping()`` and then overridden, so a test that only cares
    about one deadline still runs against whatever the environment says about
    every other knob -- which is what production does, and what patching a module
    constant silently stopped doing.
    """
    return replace(CrawlerSettings.from_mapping(), **overrides)
