"""The four formerly-direct env knobs are parsed once, at the settings home."""

from __future__ import annotations

from crawlutron.config.settings import CrawlerSettings
from tests.chokepoint_paths import REPO_ROOT


def _documented_knobs() -> dict[str, str]:
    """Live CRAWLUTRON knobs from the operator's .env.example."""
    found: dict[str, str] = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def _read(**env: str) -> CrawlerSettings:
    return CrawlerSettings.from_mapping({f"CRAWLUTRON_{k.upper()}": v for k, v in env.items()})


class TestWebhookSizeKnob:
    def test_default_is_the_declared_10mib_cap(self):
        assert _read().max_webhook_size == 10_485_760

    def test_env_override_is_parsed(self):
        assert _read(max_webhook_size="1234").max_webhook_size == 1234

    def test_zero_and_negative_hold_the_unit_floor(self):
        assert _read(max_webhook_size="0").max_webhook_size == 1
        assert _read(max_webhook_size="-5").max_webhook_size == 1

    def test_garbage_value_falls_back_without_raising(self):
        assert _read(max_webhook_size="not-an-int").max_webhook_size == 10_485_760


class TestUserAgentKnob:
    def test_default_is_empty(self):
        assert _read().user_agent == ""

    def test_override_is_stripped_once_at_the_home(self):
        assert _read(user_agent="  crawler-test/1  ").user_agent == "crawler-test/1"


class TestHeadlessKnob:
    def test_default_is_true(self):
        assert _read().headless is True

    def test_false_spellings_are_parsed_at_the_home(self):
        for raw in ("0", "false", "no", "off", "FALSE"):
            assert _read(headless=raw).headless is False

    def test_garbage_value_falls_back_to_true(self):
        assert _read(headless="maybe").headless is True


class TestWarmupUrlsKnob:
    def test_default_is_empty(self):
        assert _read().warmup_urls == ()

    def test_comma_separated_list_is_split_and_stripped(self):
        urls = _read(warmup_urls=" http://a.example/ , , http://b.example/ ").warmup_urls
        assert urls == ("http://a.example/", "http://b.example/")


def test_migrated_knobs_keep_their_documented_defaults():
    """The four migrated knobs stay in parity with the operator's .env.example."""
    documented = _documented_knobs()
    defaults = _read()
    assert defaults.max_webhook_size == int(documented["CRAWLUTRON_MAX_WEBHOOK_SIZE"])
    assert defaults.user_agent == documented["CRAWLUTRON_USER_AGENT"]
    assert defaults.headless is (documented["CRAWLUTRON_HEADLESS"].lower() == "true")
    assert defaults.warmup_urls == ()
    assert documented["CRAWLUTRON_WARMUP_URLS"] == ""
