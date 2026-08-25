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
    src = open("app/crawl/strategies/http.py").read()
    assert "10 * 1024 * 1024" in src
