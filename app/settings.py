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
from app.resource_pressure import derive_browser_pool_size, max_http_concurrency_for


# Connection-pool sizing (T7): bind the HTTP pool to the SAME RAM-derived HTTP
# concurrency the weighted admission allows, so the two can never diverge as RAM
# scales. max_http_concurrency_for() reserves the browser pool's RAM then divides
# the remainder by the per-HTTP-task estimate -- identical to the admission's
# ceiling. A larger pool would only buffer more fetched-but-unextracted pages in
# RAM and trigger admission throttling without raising throughput (CPU-bound).
_DEF_MB = 1024 * 1024


def _default_http_max_connections() -> int:
    try:
        limit = read_memory_stats().total_bytes or (4 * _DEF_MB * 1024)
    except Exception:
        limit = 4 * _DEF_MB * 1024
    # Floor of 100 keeps a usable pool on tiny/unlimited boxes; the derived value
    # (3 GiB -> 139) is the admission's real HTTP concurrency.
    return max(100, max_http_concurrency_for(limit, derive_browser_pool_size(limit)))


def _default_http_keepalive() -> int:
    # Keepalive tracks the connection ceiling (half), bounded for sanity.
    return max(20, min(128, _default_http_max_connections() // 2))


def _parse_bool_env_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CrawlerSettings:
    # Bind interface (M15): loopback by default since the service has no
    # caller authentication. Container deployments that rely on port mapping
    # must set 0.0.0.0 explicitly (the compose file does, gated by its
    # loopback-only ports mapping).
    host: str = "127.0.0.1"
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
    log_file_path: str = "./logs/browser-crawler.jsonl"
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 3
    cache_dir: str = "./cache"
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
    cookie_jar_path: str = "./cache/cookie_jar.json"
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
    # Web search provider configuration (POST /v1/search). ``search_provider``
    # selects the backend: "auto" (default) picks the first configured keyed
    # provider in priority order (tavily -> brave -> serper) and falls back to
    # the keyless DuckDuckGo HTML endpoint when none are configured. An
    # explicit provider name requires its matching ``*_API_KEY`` to be set.
    search_provider: str = "auto"
    tavily_api_key: str = ""
    brave_api_key: str = ""
    serper_api_key: str = ""
    search_limit_default: int = 5
    search_limit_max: int = 20
    search_timeout_seconds: int = 15

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> "CrawlerSettings":
        source = os.environ if values is None else values
        return cls(
            host=source.get("BROWSER_CRAWLER_HOST", cls.host),
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
            cookie_jar_path=source.get("BROWSER_CRAWLER_COOKIE_JAR_PATH", cls.cookie_jar_path),
            http_max_connections=int(source.get("BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS", cls.http_max_connections)),
            http_max_keepalive=int(source.get("BROWSER_CRAWLER_HTTP_MAX_KEEPALIVE", cls.http_max_keepalive)),
            http_keepalive_expiry=float(source.get("BROWSER_CRAWLER_HTTP_KEEPALIVE_EXPIRY", cls.http_keepalive_expiry)),
            max_browser_renders=int(source.get("BROWSER_CRAWLER_MAX_BROWSER_RENDERS", cls.max_browser_renders)),
            max_concurrent_pages=int(source.get("BROWSER_CRAWLER_MAX_CONCURRENT_PAGES", cls.max_concurrent_pages)),
            browser_pool_size=int(source.get("BROWSER_CRAWLER_BROWSER_POOL_SIZE", cls.browser_pool_size)),
            challenge_wait_seconds=int(source.get("BROWSER_CRAWLER_CHALLENGE_WAIT_SECONDS", cls.challenge_wait_seconds)),
            search_provider=source.get("BROWSER_CRAWLER_SEARCH_PROVIDER", cls.search_provider),
            tavily_api_key=source.get("BROWSER_CRAWLER_TAVILY_API_KEY", cls.tavily_api_key),
            brave_api_key=source.get("BROWSER_CRAWLER_BRAVE_API_KEY", cls.brave_api_key),
            serper_api_key=source.get("BROWSER_CRAWLER_SERPER_API_KEY", cls.serper_api_key),
            search_limit_default=int(source.get("BROWSER_CRAWLER_SEARCH_LIMIT_DEFAULT", cls.search_limit_default)),
            search_limit_max=int(source.get("BROWSER_CRAWLER_SEARCH_LIMIT_MAX", cls.search_limit_max)),
            search_timeout_seconds=int(source.get("BROWSER_CRAWLER_SEARCH_TIMEOUT_SECONDS", cls.search_timeout_seconds)),
        )
