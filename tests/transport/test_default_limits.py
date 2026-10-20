"""Memory guardrails: conservative default response size cap."""

import sys
from pathlib import Path

# Resolve the app root from this file's location so the tests are independent of the CWD they are launched
# from (repo root or any subdirectory both work): the app root is three levels up.
_APP_ROOT = Path(__file__).resolve().parents[2]


def test_default_max_content_length_is_none_so_ram_scaling_stays_live():
    """A concrete pydantic default materialized on every API request and
    silently disabled the concurrency-scaled RAM cap. The field must default
    to None (absent => RAM-scaled), keeping explicit values as true overrides.
    """
    sys.path.insert(0, str(_APP_ROOT))
    from app.models.crawl_policy_limits import CrawlPolicyLimits

    limits = CrawlPolicyLimits()
    assert limits.max_content_length_bytes is None


def test_engine_fallback_limit_matches():
    sys.path.insert(0, str(_APP_ROOT))
    from app.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

    assert DEFAULT_MAX_CONTENT_LENGTH_BYTES == 10 * 1024 * 1024
