"""Adversarial review the analytics hard-skip list must not hold content hosts:
``ANALYTICS_HOST_SUFFIXES`` silently drops links to listed hosts, so a government
domain there is data poisoning -- tracking stays skipped, content never enters.
"""

from safe_url.normalized_url import ANALYTICS_HOST_SUFFIXES

from tests._policy_factories import canonical


class TestAnalyticsSkipListBoundaries:
    def test_tracking_hosts_are_skipped(self):
        for url in (
            "https://www.google-analytics.com/analytics.js",
            "https://connect.facebook.net/en_US/sdk.js",
            "https://analytics.twitter.com/px.gif",
            "https://static.hotjar.com/c/hotjar.js",
        ):
            assert canonical(url).is_analytics_host, url

    def test_government_content_host_is_never_skipped(self):
        # Cdc.gov was in the skip list, silently dropping an entire government domain from every crawl.
        assert "cdc.gov" not in ANALYTICS_HOST_SUFFIXES
        assert not canonical("https://www.cdc.gov/flu/weekly/index.htm").is_analytics_host
        assert not canonical("https://cdc.gov/data.csv").is_analytics_host

    def test_generic_content_hosts_are_never_skipped(self):
        for url in (
            "https://www.wikipedia.org/",
            "https://www.nytimes.com/2024/01/01/us/politics.html",
            "https://example.com/",
        ):
            assert not canonical(url).is_analytics_host, url

    def test_subdomains_of_content_hosts_are_never_skipped(self):
        assert not canonical("https://data.cdc.gov/api/views.json").is_analytics_host
