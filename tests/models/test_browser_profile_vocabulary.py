"""The browser_profile value vocabularies: viewport and resource_mode are closed.

A typo'd value used to pass admission and silently default at the consumer's
equality check; the vocabulary authority refuses it instead (#314/C10).
"""

import pytest
from pydantic import ValidationError

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_policy_vocabulary import close_browser_profile_vocabulary


class TestViewportAndResourceModeAreClosed:
    """The two string values consumers narrow with ``==`` admit nothing else."""

    @pytest.mark.parametrize(
        ("key", "value", "valid"),
        [
            ("viewport", "mobil", "desktop, mobile"),
            ("resource_mode", "reducd", "full, reduced"),
        ],
    )
    def test_a_misspelled_value_is_refused_naming_the_valid_ones(self, key, value, valid):
        with pytest.raises(ValueError, match=valid):
            close_browser_profile_vocabulary({key: value})

    @pytest.mark.parametrize(
        "profile",
        [
            {"viewport": "mobile", "resource_mode": "reduced"},
            {"viewport": "desktop", "resource_mode": "full"},
            {"viewport": "mobile"},
            {"resource_mode": "reduced"},
        ],
    )
    def test_valid_values_pass_unchanged(self, profile):
        assert close_browser_profile_vocabulary(dict(profile)) == profile

    def test_absent_values_pass(self):
        assert close_browser_profile_vocabulary({"include_html": True}) == {"include_html": True}

    @pytest.mark.parametrize("key", ["viewport", "resource_mode"])
    def test_a_non_string_value_is_refused(self, key):
        with pytest.raises(ValueError, match=key):
            close_browser_profile_vocabulary({key: ["desktop"]})


class TestTheModelEdgeRefusesToo:
    """CrawlPolicy closes browser_profile through the same vocabulary authority."""

    def test_a_misspelled_viewport_is_a_validation_error(self):
        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"viewport": "mobil"})
