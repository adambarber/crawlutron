"""The PDF extraction fallback must not run on the event loop.

The sync extractor called directly would stall the whole loop for one PDF's
parse -- the same defect the to_thread sweeps killed -- so it is offloaded.
"""

from app.models.normalized_url import NormalizedUrl
from app.transport.browser import BrowserRenderer, RenderOptions
from tests._assertions import install_to_thread_spy
from tests._browser_doubles import BrowserFakePage, build_pinned_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

_PDF_URL = "http://93.184.216.34/doc.pdf"  # public IP literal: passes the final-URL SSRF re-check
# The admitted form of the fixture URL: the render body's seam is value-native.
_PDF_VALUE = NormalizedUrl(_PDF_URL, policy=UNRESTRICTED_HOST_POLICY)


class _PdfPage(BrowserFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = _PDF_URL

    async def content(self):
        return "<html></html>"

    async def evaluate(self, *a, **k):
        return ""


async def _real_extraction(page, options):
    """Un-stub the builder's canned extraction so the real branch is driven.

    ``build_pinned_renderer`` pins extraction to a success fixture; the
    PDF-offload contract needs the real branch, so both collaborators are swapped.
    """
    html = await BrowserRenderer.extract_html(page) if options.include_html else None
    text = await BrowserRenderer.extract_text(page) if options.include_text else None
    return html, text, "rendered", False


async def _looks_like_pdf(url):
    return url.lower().endswith(".pdf")


def _build_pdf_renderer(monkeypatch, page):
    return build_pinned_renderer(page, monkeypatch, extract=_real_extraction, looks_like_pdf=_looks_like_pdf)


async def _render_pdf(monkeypatch):
    """Drive the real PDF fallback with a blocking extractor at the shared URL."""
    page = _PdfPage()
    renderer = _build_pdf_renderer(monkeypatch, page)

    def blocking_pdf_extractor(url) -> str:
        # The extractor seam takes the admitted value: the value object carries the policy, and a downloader
        # projects .raw itself.
        assert url == _PDF_VALUE
        return "extracted pdf text"

    renderer._pdf_text_extractor = blocking_pdf_extractor
    return await renderer._render_with_page_unlocked(
        _PDF_VALUE,
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
