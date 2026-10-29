"""Prose-only generic phrases must not fail a page (#521).

"not found"/"forbidden"/"security check" alone are ordinary English, not verdicts;
specific variants and corroborated hits still classify.
"""

from crawlutron.extraction.classify import classify_page


def test_prose_only_not_found_phrase_stays_rendered():
    """A page narrating "not found" in prose must not fail as not_found."""
    html = "<html><body>Chapter 3 was not found in the archive. See the index.</body></html>"
    assert classify_page(html, None) == "rendered"


def test_prose_only_forbidden_phrase_stays_rendered():
    """A benign single "forbidden" mention is not access_denied."""
    html = "<html><body>The word 'forbidden' appears in chapter one.</body></html>"
    assert classify_page(html, None) == "rendered"


def test_prose_only_security_check_phrase_stays_rendered():
    """A single "security check" mention is not captcha_detected."""
    html = "<html><body>Customs run a security check on every parcel.</body></html>"
    assert classify_page(html, None) == "rendered"


def test_corroborated_not_found_still_classifies():
    """Two independent not-found signals still classify."""
    html = "<html><title>404 Not Found</title><body>page not found</body></html>"
    assert classify_page(html, None) == "not_found"


def test_corroborated_forbidden_still_classifies():
    html = "<html><body>Access denied. 403 forbidden</body></html>"
    assert classify_page(html, None) == "access_denied"
