"""Adversarial review the analytics hard-skip list must not contain
legitimate content hosts.

``ANALYTICS_HOST_SUFFIXES`` silently drops every discovered link to a listed
host (links are recorded but never enqueued, and the browser route handler
aborts their requests). A government content domain in that list is data
poisoning: ``cdc.gov`` silently disappeared from every crawl until it was
removed. These tests pin the boundary: tracking/ad infrastructure stays
skipped, real content hosts never enter the list.
"""

from app.constants import ANALYTICS_HOST_SUFFIXES, is_analytics_host
from tests._policy_factories import canonical


class TestAnalyticsSkipListBoundaries:
    def test_tracking_hosts_are_skipped(self):
        for url in (
            "https://www.google-analytics.com/analytics.js",
            "https://connect.facebook.net/en_US/sdk.js",
            "https://analytics.twitter.com/px.gif",
            "https://static.hotjar.com/c/hotjar.js",
        ):
            assert is_analytics_host(canonical(url)), url

    def test_government_content_host_is_never_skipped(self):
        # Cdc.gov was in the skip list, silently dropping an entire
        # government domain from every crawl.
        assert "cdc.gov" not in ANALYTICS_HOST_SUFFIXES
        assert not is_analytics_host(canonical("https://www.cdc.gov/flu/weekly/index.htm"))
        assert not is_analytics_host(canonical("https://cdc.gov/data.csv"))

    def test_generic_content_hosts_are_never_skipped(self):
        for url in (
            "https://www.wikipedia.org/",
            "https://www.nytimes.com/2024/01/01/us/politics.html",
            "https://example.com/",
        ):
            assert not is_analytics_host(canonical(url)), url

    def test_subdomains_of_content_hosts_are_never_skipped(self):
        assert not is_analytics_host(canonical("https://data.cdc.gov/api/views.json"))
