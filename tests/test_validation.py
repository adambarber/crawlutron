"""Deep URL normalization: sort query params, strip default ports, drop fragments."""

import unittest

from app.validation import normalize_public_http_url, registrable_domain


class DeepUrlNormalizationTest(unittest.TestCase):
    def test_sorts_query_params_and_strips_default_port_and_fragment(self) -> None:
        self.assertEqual(
            normalize_public_http_url("https://example.com:443/b/?b=2&a=1#frag"),
            "https://example.com/b?a=1&b=2",
        )

    def test_http_default_port_stripped(self) -> None:
        self.assertEqual(
            normalize_public_http_url("http://example.com:80/path/?z=1&a=2"),
            "http://example.com/path?a=2&z=1",
        )

    def test_preserves_non_default_port(self) -> None:
        self.assertEqual(
            normalize_public_http_url("https://example.com:8443/path"),
            "https://example.com:8443/path",
        )

    def test_trailing_slash_and_fragment_removed(self) -> None:
        self.assertEqual(
            normalize_public_http_url("https://example.com/"),
            "https://example.com",
        )
        self.assertEqual(
            normalize_public_http_url("https://example.com/page/#section"),
            "https://example.com/page",
        )

    def test_duplicate_keys_kept_and_sorted(self) -> None:
        self.assertEqual(
            normalize_public_http_url("https://example.com/?b=2&a=1&a=0"),
            "https://example.com?a=0&a=1&b=2",
        )


if __name__ == "__main__":
    unittest.main()
