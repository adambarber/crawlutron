"""TestValidationGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestValidationGaps:
    def test_unsupported_scheme(self):
        with pytest.raises(ValueError):
            normalize_public_http_url("ftp://example.com/")

    def test_private_network_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with pytest.raises(ValueError):
                normalize_public_http_url("http://127.0.0.1/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_trailing_colon_path(self):
        assert normalize_public_http_url("https://example.com/seg:") == "https://example.com/seg"

    def test_registrable_domain(self):
        assert registrable_domain("https://a.b.c.example.com/x") == "example.com"
        assert registrable_domain("https://example.com/x") == "example.com"

    def test_invalid_url_no_host(self):
        with pytest.raises(ValueError):
            normalize_public_http_url("http:///path")

    def test_private_network_localhost_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with pytest.raises(ValueError):
                normalize_public_http_url("http://localhost/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_private_network_normal_domain_passed_through(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            out = normalize_public_http_url("http://example.com/x")
            assert out == "http://example.com/x"
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_registrable_domain_two_part_suffix(self):
        assert registrable_domain("https://a.b.example.co.uk/x") == "example.co.uk"

    def test_registrable_domain_multi_level_suffixes(self):
        # M5: the hand-rolled 7-entry suffix set collapsed every unlisted
        # two-part suffix to itself (a.co.jp and b.co.jp both -> co.jp),
        # sharing gates across unrelated sites. The real PSL keeps them distinct.
        assert registrable_domain("https://a.co.jp/x") == "a.co.jp"
        assert registrable_domain("https://b.co.jp/x") == "b.co.jp"
        assert registrable_domain("https://www.example.com.br/x") == "example.com.br"
        assert registrable_domain("https://shop.example.gov.in/x") == "example.gov.in"

    def test_registrable_domain_falls_back_without_dataset(self, monkeypatch):
        # The heuristic fallback must keep working when the dataset is
        # absent (import safety), even though correctness needs the PSL.
        import sys

        import app.validation as validation

        monkeypatch.setitem(sys.modules, "publicsuffixlist", None)
        monkeypatch.delattr(validation, "_PSL", raising=False)
        assert validation.registrable_domain("https://a.b.example.co.uk/x") == "example.co.uk"
        assert validation.registrable_domain("https://a.b.c.example.com/x") == "example.com"


