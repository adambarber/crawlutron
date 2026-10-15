from dataclasses import replace

import app.extraction.content as _extraction_module
from app.config.settings import CrawlerSettings
from app.extraction.content import detect_pagination as _production_detect_pagination
from app.extraction.content import extract_content
from app.extraction.content import extract_discovered_links as _production_extract_discovered_links
from app.extraction.content import extract_js_endpoints as _extract_js_endpoints_with_policy
from app.extraction.content import html_diagnostics as _production_html_diagnostics
from app.extraction.document_text import format_extracted_document_text
from app.transport.document import _format_table
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


async def default_http_fetcher(*args, **kwargs):
    return await _production_default_http_fetcher(*args, **kwargs)


__all__ = [
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
