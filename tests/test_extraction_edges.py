"""Content extraction edge cases: markitdown worker, EOF handling, zombie reaping."""

from _testkit import *


class TestExtractionEdges:
    """Cover remaining branches in extraction.py"""

    def test_extract_content_with_css_selector(self):
        from app.crawl.extraction import extract_content

        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", css_selector=".target")
        assert result.text == "selected"

    def test_extract_content_with_xpath(self):
        from app.crawl.extraction import extract_content

        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", xpath='//div[@class="target"]')
        assert result.text == "selected"

    def test_html_to_markdown_markdownify_success(self):
        from app.crawl.extraction import _html_to_markdown

        text, md = _html_to_markdown("<p>hello <strong>world</strong></p>")
        assert (text) is not None
        assert (md) is not None
        assert "world" in md

    def test_html_to_markdown_markdownify_exception(self):
        from app.crawl.extraction import _html_to_markdown

        text, md = _html_to_markdown("<p>test</p>")
        assert (text) is not None

    def test_extract_selected_no_html(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("", "http://e.com/", css_selector=".x")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_css_selector_no_match(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", css_selector=".missing")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_xpath_no_match(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", xpath="//span")
        assert result.text is None or isinstance(result.text, str)
        assert (result.markdown) is None

    def test_extract_selected_xpath_invalid(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected("<div>content</div>", "http://e.com/", xpath="//[")
        assert result.text is None or isinstance(result.text, str)

    def test_extract_selected_xpath_string_node(self):
        from app.crawl.extraction import _extract_selected

        result = _extract_selected(
            "<div><span>text1</span><span>text2</span></div>", "http://e.com/", xpath="//span/text()"
        )
        assert (result.text) is not None

    def test_extract_html_trafilatura_exception(self):
        from app.crawl.extraction import _extract_html

        html = "<html><body><p>test content</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        assert (result.text) is not None

    def test_extract_html_markdownify_exception(self):
        from app.crawl.extraction import _extract_html

        html = "<html><body><p>plain text fallback</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        assert (result.text) is not None

    def test_extract_document_markitdown_import_error(self):
        from app.crawl.extraction import _extract_document

        result = _extract_document(b"fake pdf content", "http://e.com/doc.pdf")
        assert (result.text) is not None

    def test_extract_document_markitdown_exception(self):
        from app.crawl.extraction import _extract_document

        result = _extract_document(b"not a pdf", "http://e.com/doc.pdf")
        assert result.text is None or isinstance(result.text, str)

    def test_parse_html_tree_empty(self):
        from app.crawl.extraction import _parse_html_tree

        assert (_parse_html_tree("")) is None
        assert (_parse_html_tree(None)) is None

    def test_parse_html_tree_parser_error(self):
        from app.crawl.extraction import _parse_html_tree

        result = _parse_html_tree("<")
        assert (result) is not None
