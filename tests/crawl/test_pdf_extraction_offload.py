"""The PDF extraction fallback must not run on the event loop.

The render path ends with a PDF fallback (browser produced no text for a
``.pdf`` URL). The extractor is a sync downloader/parser -- calling it
directly inside ``_render_with_page_unlocked`` stalls the WHOLE loop for one
PDF's parse, the same defect family the to_thread sweeps killed three times
(doc-pool). ``install_to_thread_spy`` records offloads without
changing behavior, so this is a behavioral assertion on the real render
body: the branch runs (extraction_method ``pdf_text``) and its extractor is
recorded as offloaded.
"""

from app.crawl.strategies.browser import BrowserRenderer, RenderOptions
from tests._assertions import install_to_thread_spy
from tests._browser_doubles import BrowserFakePage, build_pinned_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

_PDF_URL = "http://93.184.216.34/doc.pdf"  # public IP literal: passes the final-URL SSRF re-check


class _PdfPage(BrowserFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = _PDF_URL

    async def content(self):
        return "<html></html>"

    async def evaluate(self, *a, **k):
        return ""

    async def wait_for_load_state(self, state, timeout=None):
        return None


async def _real_extraction(self, page, options):
    """Un-stub the builder's canned extraction so the real branch is driven.

    ``build_pinned_renderer`` pins ``_extract_with_challenge_wait`` to a
    success fixture and disables ``_looks_like_pdf``; the PDF-offload contract
    needs the REAL branch, so both stubs are re-enabled here (the shared
    builder's defaults are deliberately preserved for every other consumer).
    """
    html = await BrowserRenderer.extract_html(self, page)
    text = await BrowserRenderer._extract_text(self, page)
    return html, text, "rendered", False


def _build_pdf_renderer(monkeypatch, page):
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(BrowserRenderer, "_extract_with_challenge_wait", _real_extraction)
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: url.lower().endswith(".pdf"))
    return renderer


async def _render_pdf(monkeypatch):
    """Drive the real PDF fallback with a blocking extractor at the shared URL."""
    page = _PdfPage()
    renderer = _build_pdf_renderer(monkeypatch, page)

    def blocking_pdf_extractor(url: str, policy) -> str:
        assert url == _PDF_URL
        assert policy == UNRESTRICTED_HOST_POLICY
        return "extracted pdf text"

    renderer._pdf_text_extractor = blocking_pdf_extractor
    return await renderer._render_with_page_unlocked(
        _PDF_URL,
        RenderOptions(include_text=True),
        policy=UNRESTRICTED_HOST_POLICY,
    )


class TestPdfExtractorIsOffloaded:
    async def test_pdf_fallback_runs_in_worker_thread(self, monkeypatch):
        seen = install_to_thread_spy(monkeypatch)

        result = await _render_pdf(monkeypatch)

        assert (result.metadata or {}).get("extraction_method") == "pdf_text", (
            "the PDF fallback branch never ran -- the test would pass vacuously"
        )
        assert "blocking_pdf_extractor" in seen, (
            "pdf extractor ran on the event loop: expected an asyncio.to_thread offload"
        )

    async def test_result_text_comes_from_the_extractor(self, monkeypatch):
        result = await _render_pdf(monkeypatch)
        assert (result.metadata or {}).get("extraction_method") == "pdf_text"
        assert result.text == "extracted pdf text"
