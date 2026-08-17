from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _parse_bool_env_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CrawlerSettings:
    host_port: int = 8765
    global_concurrency: int = 4
    per_domain_concurrency: int = 1
    per_domain_delay_ms: int = 1000
    max_batch_size: int = 50
    default_timeout_seconds: int = 30
    default_max_retries: int = 2
    respect_robots: bool = True
    allowed_hosts: str = ""
    denied_hosts: str = ""
    log_level: str = "info"
    log_file_path: str = "/app/logs/browser-crawler.jsonl"
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 3

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> "CrawlerSettings":
        source = os.environ if values is None else values
        return cls(
            host_port=int(source.get("BROWSER_CRAWLER_HOST_PORT", cls.host_port)),
            global_concurrency=int(source.get("BROWSER_CRAWLER_GLOBAL_CONCURRENCY", cls.global_concurrency)),
            per_domain_concurrency=int(source.get("BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY", cls.per_domain_concurrency)),
            per_domain_delay_ms=int(source.get("BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS", cls.per_domain_delay_ms)),
            max_batch_size=int(source.get("BROWSER_CRAWLER_MAX_BATCH_SIZE", cls.max_batch_size)),
            default_timeout_seconds=int(source.get("BROWSER_CRAWLER_DEFAULT_TIMEOUT_SECONDS", cls.default_timeout_seconds)),
            default_max_retries=int(source.get("BROWSER_CRAWLER_DEFAULT_MAX_RETRIES", cls.default_max_retries)),
            respect_robots=_parse_bool_env_value(source.get("BROWSER_CRAWLER_RESPECT_ROBOTS", str(cls.respect_robots))),
            allowed_hosts=source.get("BROWSER_CRAWLER_ALLOWED_HOSTS", cls.allowed_hosts),
            denied_hosts=source.get("BROWSER_CRAWLER_DENIED_HOSTS", cls.denied_hosts),
            log_level=source.get("BROWSER_CRAWLER_LOG_LEVEL", cls.log_level),
            log_file_path=source.get("BROWSER_CRAWLER_LOG_FILE", cls.log_file_path),
            log_file_max_bytes=int(source.get("BROWSER_CRAWLER_LOG_MAX_BYTES", cls.log_file_max_bytes)),
            log_file_backup_count=int(source.get("BROWSER_CRAWLER_LOG_BACKUP_COUNT", cls.log_file_backup_count)),
        )
