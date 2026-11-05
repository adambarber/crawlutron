"""Service identity is coherent: every wire surface reports the version the
installed package carries, derived once in the vocabulary and re-derived nowhere.
"""

from crawlutron import __version__
from crawlutron.config.settings import CrawlerSettings
from crawlutron.discovery.agent_hints import STRATEGY_DESCRIPTIONS
from crawlutron.discovery.capabilities import capabilities_payload
from crawlutron.discovery.well_known import discovery_payload
from crawlutron.mcp.protocol import server_info
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.json_value import JsonValue
from crawlutron.vocabulary.identity import DEFAULT_USER_AGENT, SERVICE_VERSION
from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET


def test_service_version_is_the_package_version():
    assert SERVICE_VERSION == __version__


def test_every_wire_surface_reports_the_package_version():
    ua_version: str = DEFAULT_USER_AGENT.split("/")[1].split(" ")[0]
    mcp_info = server_info()
    assert isinstance(mcp_info, dict)
    surfaces: dict[str, JsonValue] = {
        "capabilities": capabilities_payload(CrawlerSettings.from_mapping({}))["version"],
        "well_known": discovery_payload(CrawlerSettings.from_mapping({}))["version"],
        "mcp_server_info": mcp_info["version"],
        "user_agent": ua_version,
    }
    wrong = {name: value for name, value in surfaces.items() if value != __version__}
    assert not wrong, f"wire surfaces reporting a version the package does not carry: {wrong}"


def test_user_agent_names_the_service():
    from crawlutron.vocabulary.identity import SERVICE_ID

    assert DEFAULT_USER_AGENT.startswith(f"{SERVICE_ID}/")


def test_hints_impersonation_matches_code():
    assert CFFI_IMPERSONATE_TARGET in STRATEGY_DESCRIPTIONS[CrawlStrategy.HTTP_CFFI_IMPERSONATE]
