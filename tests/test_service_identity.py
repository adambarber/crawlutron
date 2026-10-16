"""Service identity must be coherent, from one source of truth: the User-Agent
version and the agent hints drifted from the API version and the actual
impersonation target, and both now derive from shared constants.
"""

from app.discovery.agent_hints import STRATEGY_DESCRIPTIONS
from app.models.crawl_strategy import CrawlStrategy
from app.vocabulary.identity import DEFAULT_USER_AGENT, SERVICE_VERSION
from app.vocabulary.strategies import CFFI_IMPERSONATE_TARGET


def test_user_agent_tracks_api_version():
    assert SERVICE_VERSION in DEFAULT_USER_AGENT


def test_hints_impersonation_matches_code():
    assert CFFI_IMPERSONATE_TARGET in STRATEGY_DESCRIPTIONS[CrawlStrategy.HTTP_CFFI_IMPERSONATE]
