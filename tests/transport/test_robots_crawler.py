from __future__ import annotations

from app.config.settings import CrawlerSettings
from app.orchestration.engine import crawl_request
from app.transport.robots import robots_allows
from app.transport.robots_parse import parse_robots_rules
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetcher_factories import routed_fetcher
from tests._policy_factories import canonical


def _make_fetcher(robots_status: int, robots_body: str = ""):
    return routed_fetcher(
        {"/robots.txt": (robots_status, robots_body, "text/plain")},
        default=(200, b"<html><body>hi</body></html>", "text/html"),
    )


def _policy():
    return {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 0,
            "max_discovered_links": 10,
            "respect_robots": True,
        },
    }


async def _crawl_page(host: str, fetcher):
    settings = CrawlerSettings.from_mapping()
    result = await crawl_request(
        {"url": f"http://{host}/page", "policy": _policy()},
        settings=settings,
        http_fetcher=fetcher,
    )
    return result["results"][0]


class TestRobotsFailClosed:
    async def test_robots_5xx_disallows(self):
        item = await _crawl_page("robots-fail.test", _make_fetcher(500))
        assert item.classification == "robots_disallowed"
        assert item.status != "success"

    async def test_robots_404_allows(self):
        item = await _crawl_page("robots-absent.test", _make_fetcher(404))
        assert item.status == "success"

    async def test_group_selection_uses_product_token(self):
        # "github" appears in the full User-Agent URL but not in the product token: the github group must not
        # apply to us; the * group allows.
        body = "User-agent: github\nDisallow: /\nUser-agent: *\nAllow: /\n"
        item = await _crawl_page("robots-token.test", _make_fetcher(200, body))
        assert item.status == "success"


class TestRobotsDenialSaysWhy:
    """#398: a refusal and a forbidding policy both deny, and the caller is told which.

    A site that refused to serve its robots policy is not a site that forbids
    the path: the first is a site problem to report, the second a route to skip.
    """

    async def test_a_refused_robots_fetch_is_not_reported_as_a_forbidding_policy(self):
        item = await _crawl_page("robots-refused.test", _make_fetcher(403))
        assert item.classification == "robots_disallowed"
        assert item.error == "robots_txt_refused"

    async def test_a_forbidding_policy_is_reported_as_such(self):
        item = await _crawl_page("robots-forbid.test", _make_fetcher(200, "User-agent: *\nDisallow: /\n"))
        assert item.classification == "robots_disallowed"
        assert item.error == "robots_txt_disallowed"

    async def test_an_unreachable_policy_is_reported_as_such(self):
        item = await _crawl_page("robots-down.test", _make_fetcher(500))
        assert item.classification == "robots_disallowed"
        assert item.error == "robots_txt_unreachable"

    async def test_the_denial_names_the_source_in_its_diagnostics(self):
        item = await _crawl_page("robots-refused.test", _make_fetcher(403))
        assert item.diagnostics["robots"]["evaluation"] == "refused"


class TestEmptyDisallowDoesNotEraseItsGroup:
    """RFC 9309 2.2.2: an empty ``Disallow`` value is a rule to ignore, not a verdict."""

    def test_an_empty_disallow_does_not_erase_the_other_rules_in_its_group(self):
        """The matcher used to allow-all on sight of an empty ``Disallow``.

        It never read the rules that followed, so ``Disallow:`` next to
        ``Disallow: /private`` allowed everything."""
        both = parse_robots_rules(
            "User-agent: *\nDisallow:\nDisallow: /private\n", token=HTTP_DECLARED_CRAWLER
        )
        assert both == [(False, ""), (False, "/private")]
        assert robots_allows(canonical("https://example.com/private/secret"), both) is False
        assert robots_allows(canonical("https://example.com/public"), both) is True

    def test_a_group_whose_only_rule_is_the_empty_disallow_allows_everything(self):
        """The form keeps the meaning it has on its own.

        A group declaring nothing but ``Disallow:`` allows everything -- the
        shortcut was never needed to express it."""
        only_empty = parse_robots_rules("User-agent: *\nDisallow:\n", token=HTTP_DECLARED_CRAWLER)
        assert only_empty == [(False, "")]
        assert robots_allows(canonical("https://example.com/private/secret"), only_empty) is True

    def test_an_allow_still_beats_a_longer_disallow_in_the_same_group(self):
        both = parse_robots_rules(
            "User-agent: *\nDisallow:\nDisallow: /private\nAllow: /private/ok\n",
            token=HTTP_DECLARED_CRAWLER,
        )
        assert robots_allows(canonical("https://example.com/private/ok"), both) is True
        assert robots_allows(canonical("https://example.com/private/other"), both) is False
