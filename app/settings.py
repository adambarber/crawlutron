from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from app.constants import (
    DEFAULT_BROWSER_POOL_SIZE,
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_MAX_BROWSER_RENDERS,
    DEFAULT_MAX_CONCURRENT_PAGES,
)
from app.memory_pressure import read_memory_stats


# Connection-pool sizing (T7): scale the HTTP pool with the cgroup memory limit
# instead of a fixed cap, but size it to the ACTUAL RAM-derived HTTP concurrency
# (not an over-provisioned value). ~22 MiB per limit byte approximates the
# browser-pool reserve plus ~12 MiB per in-flight HTTP task, so 3 GiB -> ~139
# connections -- matching what the weighted admission actually allows. A larger
# pool only buffers more fetched-but-unextracted pages in RAM and triggers
# admission throttling without raising throughput (the crawler is CPU-bound).
_DEF_MB = 1024 * 1024


def _default_http_max_connections() -> int:
    try:
        limit = read_memory_stats().total_bytes or (4 * _DEF_MB * 1024)
    except Exception:
        limit = 4 * _DEF_MB * 1024
    # ~22 MiB/limit -> 3 GiB ~= 139, capped so a single container never opens
    # an unbounded number.
    return max(100, min(512, int(limit // (22 * _DEF_MB))))


def _default_http_keepalive() -> int:
    try:
        limit = read_memory_stats().total_bytes or (4 * _DEF_MB * 1024)
    except Exception:
        limit = 4 * _DEF_MB * 1024
    return max(20, min(128, int(limit // (64 * _DEF_MB))))


def _parse_bool_env_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CrawlerSettings:
    host_port: int = 8765
    global_concurrency: int = 4
    per_domain_concurrency: int = 2
    per_domain_delay_ms: int = 500
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
    cache_dir: str = "/app/cache"
    cache_ttl_seconds: int = 3600
    # Opt-in disk content cache (#5). Disabled by default so per-URL content is
    # always freshly fetched unless explicitly enabled (e.g. in the container image).
    content_cache_enabled: bool = False
    # Adaptive throttling (#6): per-domain delay is driven by observed latency.
    adaptive_throttling: bool = True
    adaptive_min_delay: float = 0.5
    adaptive_max_delay: float = 10.0
    adaptive_latency_factor: float = 1.0
    cookie_persistence_enabled: bool = False
    cookie_jar_path: str = "/app/cache/cookie_jar.json"
    # HTTP client connection pool tuning
    http_max_connections: int = _default_http_max_connections()
    http_max_keepalive: int = _default_http_keepalive()
    http_keepalive_expiry: float = 60.0
    # Browser runtime limits (previously read from raw BROWSER_CRAWLER_*
    # env vars inside browser.py; centralized here so they share the env
    # mapping and are visible in /v1/capabilities-style config).
    max_browser_renders: int = DEFAULT_MAX_BROWSER_RENDERS
    max_concurrent_pages: int = DEFAULT_MAX_CONCURRENT_PAGES
    browser_pool_size: int = DEFAULT_BROWSER_POOL_SIZE
    challenge_wait_seconds: int = DEFAULT_CHALLENGE_WAIT_SECONDS

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
            cache_dir=source.get("BROWSER_CRAWLER_CACHE_DIR", cls.cache_dir),
            cache_ttl_seconds=int(source.get("BROWSER_CRAWLER_CACHE_TTL_SECONDS", cls.cache_ttl_seconds)),
            content_cache_enabled=_parse_bool_env_value(
                source.get("BROWSER_CRAWLER_CONTENT_CACHE_ENABLED", str(cls.content_cache_enabled))
            ),
            adaptive_throttling=_parse_bool_env_value(
                source.get("BROWSER_CRAWLER_ADAPTIVE_THROTTLING", str(cls.adaptive_throttling))
            ),
            adaptive_min_delay=float(source.get("BROWSER_CRAWLER_ADAPTIVE_MIN_DELAY", cls.adaptive_min_delay)),
            adaptive_max_delay=float(source.get("BROWSER_CRAWLER_ADAPTIVE_MAX_DELAY", cls.adaptive_max_delay)),
            adaptive_latency_factor=float(source.get("BROWSER_CRAWLER_ADAPTIVE_LATENCY_FACTOR", cls.adaptive_latency_factor)),
            cookie_persistence_enabled=_parse_bool_env_value(
                source.get("BROWSER_CRAWLER_COOKIE_PERSISTENCE_ENABLED", "false")
            ),
            cookie_jar_path=source.get("BROWSER_CRAWLER_COOKIE_JAR_PATH", "/app/cache/cookie_jar.json"),
            http_max_connections=int(source.get("BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS", cls.http_max_connections)),
            http_max_keepalive=int(source.get("BROWSER_CRAWLER_HTTP_MAX_KEEPALIVE", cls.http_max_keepalive)),
            http_keepalive_expiry=float(source.get("BROWSER_CRAWLER_HTTP_KEEPALIVE_EXPIRY", cls.http_keepalive_expiry)),
            max_browser_renders=int(source.get("BROWSER_CRAWLER_MAX_BROWSER_RENDERS", cls.max_browser_renders)),
            max_concurrent_pages=int(source.get("BROWSER_CRAWLER_MAX_CONCURRENT_PAGES", cls.max_concurrent_pages)),
            browser_pool_size=int(source.get("BROWSER_CRAWLER_BROWSER_POOL_SIZE", cls.browser_pool_size)),
            challenge_wait_seconds=int(source.get("BROWSER_CRAWLER_CHALLENGE_WAIT_SECONDS", cls.challenge_wait_seconds)),
        )
