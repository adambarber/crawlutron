"""Selector syntax validation: a malformed ``css_selector``/``xpath`` is refused at
admission as ``invalid_policy``, never parked until extraction as a flaky error. #219
"""

import pytest
from pydantic import ValidationError

from app.models.crawl_policy import CrawlPolicy


@pytest.mark.parametrize(
    "bad_css",
    [
        "div>>>",  # dangling combinator
        "a[href",  # unterminated attribute bracket
        ".x >",  # trailing combinator
    ],
    ids=["dangling-combinator", "unterminated-bracket", "trailing-combinator"],
)
def test_malformed_css_selector_raises_validation_error(bad_css):
    with pytest.raises(ValidationError, match="css_selector"):
        CrawlPolicy(css_selector=bad_css)


@pytest.mark.parametrize(
    "bad_xpath",
    [
        "//[",  # unterminated predicate
        "/foo/",  # trailing slash in an XPath location path
        "//div[contains(@class, 'x]",  # unterminated string literal
    ],
    ids=["unterminated-predicate", "trailing-slash", "unterminated-string"],
)
def test_malformed_xpath_raises_validation_error(bad_xpath):
    with pytest.raises(ValidationError, match="xpath"):
        CrawlPolicy(xpath=bad_xpath)


def test_valid_css_selector_accepted():
    policy = CrawlPolicy(css_selector="div.note > p#lead")
    assert policy.css_selector == "div.note > p#lead"


def test_valid_xpath_accepted():
    policy = CrawlPolicy(xpath="//div[@class='content']//p[1]/text()")
    assert policy.xpath == "//div[@class='content']//p[1]/text()"
