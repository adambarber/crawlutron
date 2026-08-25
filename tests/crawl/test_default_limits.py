"""Memory guardrails: conservative default response size cap."""


def test_default_max_content_length_is_10mb():
    import sys
    sys.path.insert(0, ".")
    from app.api_models import CrawlPolicyLimits
    limits = CrawlPolicyLimits()
    assert limits.max_content_length_bytes == 10 * 1024 * 1024


def test_engine_fallback_limit_matches():
    import sys
    sys.path.insert(0, ".")
    from app.api_models import CrawlPolicyLimits
    from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES

    # The 10MB default lives in ONE place (app.constants) and is referenced by
    # the http strategy instead of being hard-coded a second time.
    assert DEFAULT_MAX_CONTENT_LENGTH_BYTES == 10 * 1024 * 1024
    src = open("app/crawl/strategies/http.py").read()
    assert "DEFAULT_MAX_CONTENT_LENGTH_BYTES" in src
