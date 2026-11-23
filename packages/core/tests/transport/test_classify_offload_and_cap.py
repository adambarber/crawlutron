"""The classifier runs off the event loop and the HTML haystack is capped at the
single MAX_HTML_INPUT_BYTES door (truncate_html_input). No second cap is declared.
"""

import threading

from crawlutron.extraction.classify import classify_page
from crawlutron.safety.bounded_io import MAX_HTML_INPUT_BYTES, truncate_html_input

_MARKER = "<script>_cf_chl_opt</script>"


async def test_classify_runs_off_event_loop(monkeypatch):
    """classify_page must not run the CPU classifier on the event-loop thread."""
    main_thread = threading.get_ident()
    captured: dict[str, int] = {}

    def probe(html=None, text=None, *, title=None):
        captured["thread"] = threading.get_ident()
        from crawlutron.models.taxonomy import Classification

        return Classification.rendered, []

    monkeypatch.setattr("crawlutron.extraction.classify.classify_page_with_evidence", probe)
    await classify_page("<html><body>hi</body></html>", "hi")
    assert captured["thread"] != main_thread


async def test_oversized_html_is_truncated_before_classify():
    """A structural marker placed beyond the cap is dropped by truncation."""
    over = "a" * (MAX_HTML_INPUT_BYTES + 1000) + _MARKER
    assert len(over.encode("utf-8")) > MAX_HTML_INPUT_BYTES
    assert await classify_page(over, "x") == "rendered"


async def test_html_at_cap_still_detects_marker():
    """Input exactly at the cap is not truncated and the marker still classifies."""
    at_cap = _MARKER + "a" * (MAX_HTML_INPUT_BYTES - len(_MARKER.encode("utf-8")))
    assert len(at_cap.encode("utf-8")) == MAX_HTML_INPUT_BYTES
    assert truncate_html_input(at_cap) == at_cap
    assert await classify_page(at_cap, "x") == "bot_challenge_detected"


async def test_empty_extraction_fallback_is_binary_no_text():
    """Empty text and html still yield binary_no_text; truncation of None is safe."""
    assert await classify_page(None, "") == "binary_no_text"
