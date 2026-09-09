"""L2: service identity must be coherent (single source of truth).

The User-Agent version drifted from the API version, and the agent hints
advertised a different impersonation target than the code uses. Both now
derive from shared constants.
"""

from app.api_models import API_VERSION, CrawlStrategy
from app.constants import CFFI_IMPERSONATE_TARGET, DEFAULT_USER_AGENT
from app.discovery.agent_hints import STRATEGY_DESCRIPTIONS


def test_user_agent_tracks_api_version():
    assert API_VERSION in DEFAULT_USER_AGENT


def test_hints_impersonation_matches_code():
    assert CFFI_IMPERSONATE_TARGET in STRATEGY_DESCRIPTIONS[CrawlStrategy.HTTP_CFFI_IMPERSONATE]
