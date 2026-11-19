"""Pipeline settings are constructed once per run, not once per extraction.

The old per-call ``settings or CrawlerSettings.from_mapping()`` fallback made
every document extraction re-walk the env and re-read cgroup sysfs.
"""

from crawlutron.models.json_value import JsonValue
from tests._crawl_runners import build_payload


def _payload() -> dict[str, JsonValue]:
    return build_payload(
        url="http://example.com/doc.csv",
        strategies=["HTTP_DECLARED_CRAWLER"],
        limits={"timeout_seconds": 10, "max_retries": 0, "max_depth": 0, "max_pages": 1},
        policy={"extraction": "document"},
    )


__all__ = ["_payload"]
