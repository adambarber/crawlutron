"""Deep URL normalization: sort query params, strip default ports, drop fragments."""

import pytest

from app.validation import normalize_public_http_url


class TestDeepUrlNormalization:
    def test_sorts_query_params_and_strips_default_port_and_fragment(self):
        assert (
            normalize_public_http_url("https://example.com:443/b/?b=2&a=1#frag")
            == "https://example.com/b?a=1&b=2"
        )

    def test_http_default_port_stripped(self):
        assert (
            normalize_public_http_url("http://example.com:80/path/?z=1&a=2")
            == "http://example.com/path?a=2&z=1"
        )

    def test_preserves_non_default_port(self):
        assert normalize_public_http_url("https://example.com:8443/path") == "https://example.com:8443/path"

    def test_trailing_slash_and_fragment_removed(self):
        assert normalize_public_http_url("https://example.com/") == "https://example.com"
        assert normalize_public_http_url("https://example.com/page/#section") == "https://example.com/page"

    def test_duplicate_keys_kept_and_sorted(self):
        assert (
            normalize_public_http_url("https://example.com/?b=2&a=1&a=0") == "https://example.com?a=0&a=1&b=2"
        )


class TestNormalizationGuardMemoization:
    """Per-anchor normalization must not re-derive the env decision or the
    host SSRF verdict on every call (perf tier-2 T2): the env read is keyed
    on its raw value and the host verdict is a pure function of the host."""

    URL = "https://MEMO-T2-9E16C.TEST:443/path/?b=2&utm_source=x&a=1#frag"

    def _spy(self, monkeypatch):
        import ipaddress

        import app.validation as validation

        counts = {"env": 0, "ip": 0}

        validation._reset_guard_memo()
        real_bool = validation.parse_bool_env
        real_ip = ipaddress.ip_address

        def counting_bool(name, default=False):
            counts["env"] += 1
            return real_bool(name, default)

        class CountingIpModule:
            @staticmethod
            def ip_address(value):
                counts["ip"] += 1
                return real_ip(value)

        monkeypatch.setattr(validation, "parse_bool_env", counting_bool)
        monkeypatch.setattr(validation, "ipaddress", CountingIpModule)
        return counts

    def test_repeat_call_reuses_env_decision_and_host_verdict(self, monkeypatch):
        import app.validation as validation

        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        counts = self._spy(monkeypatch)
        first = validation.normalize_public_http_url(self.URL)
        env_after_first = counts["env"]
        ip_after_first = counts["ip"]
        second = validation.normalize_public_http_url(self.URL)
        assert second == first == "https://memo-t2-9e16c.test/path?a=1&b=2"
        # The guard really ran on the first call, then nothing re-ran on the second.
        assert env_after_first >= 1
        assert ip_after_first >= 1
        # The second identical call consulted env and the IP classifier zero
        # extra times: both decisions are memoized.
        assert counts["env"] == env_after_first
        assert counts["ip"] == ip_after_first

    def test_env_flip_between_calls_still_visible(self, monkeypatch):
        import app.validation as validation

        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "false")
        with pytest.raises(ValueError, match="private_network_target"):
            validation.normalize_public_http_url("http://localhost/x")
        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "true")
        assert validation.normalize_public_http_url("http://localhost/x") == "http://localhost/x"

    def test_unrecognized_env_value_keeps_guard_on(self, monkeypatch):
        import app.validation as validation

        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "affirmative")
        with pytest.raises(ValueError, match="private_network_target"):
            validation.normalize_public_http_url("http://localhost/x")

    def test_numeric_host_forms_still_classified(self, monkeypatch):
        import app.validation as validation

        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
        for host in ("2130706433", "0x7f000001"):
            with pytest.raises(ValueError, match="private_network_target"):
                validation.normalize_public_http_url(f"http://{host}/x")
        # Repeated forms hit the memo but must keep rejecting.
        with pytest.raises(ValueError, match="private_network_target"):
            validation.normalize_public_http_url("http://2130706433/x")
