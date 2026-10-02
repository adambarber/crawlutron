from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.constants import (
    DEFAULT_BROWSER_POOL_SIZE,
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
    DEFAULT_EXTRACTION_DOC_ACQUIRE_TIMEOUT,
    DEFAULT_EXTRACTION_DOC_TIMEOUT,
    DEFAULT_EXTRACTION_MAX_MARKDOWN,
    DEFAULT_EXTRACTION_SELECTOR_TIMEOUT,
    DEFAULT_LAZY_SCROLL_DELAY_SECONDS,
    DEFAULT_LAZY_SCROLL_STEPS,
    DEFAULT_MAX_BROWSER_RENDERS,
    DEFAULT_MAX_CONCURRENT_PAGES,
    DEFAULT_MAX_RETRIES,
    MAX_HTTP_TIMEOUT_SECONDS,
    MAX_RETRIES,
)
from app.env import get_mapping_float, get_mapping_int, in_range, parse_bool_value
from app.memory_pressure import read_memory_stats
from app.resource_pressure import derive_browser_pool_size, max_http_concurrency_for

# Connection-pool sizing: bind the HTTP pool to the SAME RAM-derived HTTP
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


ENVIRONMENT_FIELD_ALIASES: dict[str, str] = {
    "browser_cookie_harvest_timeout_seconds": "CRAWLUTRON_COOKIE_HARVEST_TIMEOUT_SECONDS",
    "browser_lazy_scroll_delay_seconds": "CRAWLUTRON_LAZY_SCROLL_DELAY_SECONDS",
    "browser_lazy_scroll_steps": "CRAWLUTRON_LAZY_SCROLL_STEPS",
    "log_file_backup_count": "CRAWLUTRON_LOG_BACKUP_COUNT",
    "log_file_path": "CRAWLUTRON_LOG_FILE",
    "log_file_max_bytes": "CRAWLUTRON_LOG_MAX_BYTES",
}


@dataclass(frozen=True)
class CrawlerSettings:
    # Bind interface: loopback by default since the service has no
    # caller authentication. Container deployments that rely on port mapping
    # must set 0.0.0.0 explicitly (the compose file does, gated by its
    # loopback-only ports mapping).
    host: str = "127.0.0.1"
    global_concurrency: int = 4
    per_domain_concurrency: int = 2
    per_domain_delay_ms: int = 500
    max_batch_size: int = 50
    default_timeout_seconds: int = 30
    default_max_retries: int = DEFAULT_MAX_RETRIES
    respect_robots: bool = True
    allowed_hosts: str = ""
    denied_hosts: str = ""
    log_level: str = "info"
    log_file_path: str = "./logs/crawlutron.jsonl"
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 3
    cache_dir: str = "./cache"
    cache_ttl_seconds: int = 3600
    # Opt-in disk content cache. Disabled by default so per-URL content is
    # always freshly fetched unless explicitly enabled (e.g. in the container image).
    content_cache_enabled: bool = False
    # Adaptive throttling: per-domain delay is driven by observed latency.
    adaptive_throttling: bool = True
    adaptive_min_delay: float = 0.5
    adaptive_max_delay: float = 10.0
    adaptive_latency_factor: float = 1.0
    cookie_persistence_enabled: bool = False
    cookie_jar_path: str = "./cache/cookie_jar.json"
    # HTTP client connection pool tuning. ``default_factory``, not a call in the
    # class body: the derivation READS the memory limit, so a class-body call was
    # import-time I/O, frozen for the process -- a box whose limit only became
    # readable after the first import kept that wrong ceiling forever. A
    # default_factory field also has no class attribute, so from_mapping calls
    # the same factory instead of reading one off the class.
    http_max_connections: int = field(default_factory=_default_http_max_connections)
    http_max_keepalive: int = field(default_factory=_default_http_keepalive)
    http_keepalive_expiry: float = 60.0
    # Browser runtime limits (previously read from raw CRAWLUTRON_*
    # env vars inside browser.py; centralized here so they share the env
    # mapping and are visible in /v1/capabilities-style config).
    max_browser_renders: int = DEFAULT_MAX_BROWSER_RENDERS
    max_concurrent_pages: int = DEFAULT_MAX_CONCURRENT_PAGES
    browser_pool_size: int = DEFAULT_BROWSER_POOL_SIZE
    challenge_wait_seconds: int = DEFAULT_CHALLENGE_WAIT_SECONDS
    # Browser fixed-overhead knobs: configurability
    # only -- defaults equal the hardcoded behavior (3 steps / 1s / 5.0s).
    # Per-request ``browser_profile`` keys (``lazy_scroll_steps``,
    # ``lazy_scroll_delay_seconds``, ``cookie_harvest_timeout_seconds``)
    # override these per render.
    browser_lazy_scroll_steps: int = DEFAULT_LAZY_SCROLL_STEPS
    browser_lazy_scroll_delay_seconds: float = DEFAULT_LAZY_SCROLL_DELAY_SECONDS
    browser_cookie_harvest_timeout_seconds: float = DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS
    # Extraction deadlines (previously read from raw CRAWLUTRON_* env vars inside
    # extraction.py and frozen there at import; centralized here for the same
    # reason the browser ones above are). A deadline a caller cannot construct is
    # a deadline only an operator can move, and only before the process starts.
    extraction_doc_timeout: float = DEFAULT_EXTRACTION_DOC_TIMEOUT
    extraction_selector_timeout: float = DEFAULT_EXTRACTION_SELECTOR_TIMEOUT
    extraction_doc_acquire_timeout: float = DEFAULT_EXTRACTION_DOC_ACQUIRE_TIMEOUT
    extraction_max_markdown: int = DEFAULT_EXTRACTION_MAX_MARKDOWN
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
    def from_mapping(cls, values: Mapping[str, str] | None = None) -> CrawlerSettings:
        """Build settings from a mapping (os.environ by default).

        Every numeric read goes through the never-raise env chokepoint
        (``get_mapping_int``/``get_mapping_float``): a typo'd value falls
        back to the dataclass default with a warning instead of crashing
        boot, and every boolean goes through ``parse_bool_value`` so a typo
        can never silently flip a policy open (``RESPECT_ROBOTS=ture``
        keeps robots respected).
        """
        source = os.environ if values is None else values
        return cls(
            host=source.get("CRAWLUTRON_HOST", cls.host),
            global_concurrency=get_mapping_int(
                source, "CRAWLUTRON_GLOBAL_CONCURRENCY", cls.global_concurrency
            ),
            per_domain_concurrency=get_mapping_int(
                source, "CRAWLUTRON_PER_DOMAIN_CONCURRENCY", cls.per_domain_concurrency
            ),
            per_domain_delay_ms=get_mapping_int(
                source, "CRAWLUTRON_PER_DOMAIN_DELAY_MS", cls.per_domain_delay_ms
            ),
            max_batch_size=get_mapping_int(source, "CRAWLUTRON_MAX_BATCH_SIZE", cls.max_batch_size),
            default_timeout_seconds=get_mapping_int(
                source,
                "CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS",
                cls.default_timeout_seconds,
                # Bounded by the same declaration the policy is validated
                # against, so an operator config cannot install a per-request
                # default that every crawl would then be rejected for. Out of
                # range is REJECTED here, loudly, rather than clamped into a
                # number nobody configured: the comment used to promise that and
                # the code silently clamped.
                lo=1,
                hi=MAX_HTTP_TIMEOUT_SECONDS,
                validate=in_range(1, MAX_HTTP_TIMEOUT_SECONDS),
                validate_event="operator_timeout_out_of_range",
            ),
            default_max_retries=get_mapping_int(
                source,
                "CRAWLUTRON_DEFAULT_MAX_RETRIES",
                cls.default_max_retries,
                # The deployment's ceiling, not the per-job one: this default
                # applies to every caller, so it is capped at MAX_RETRIES and an
                # operator asking for more is told so rather than handed a
                # different number than they configured.
                lo=0,
                hi=MAX_RETRIES,
                validate=in_range(0, MAX_RETRIES),
                validate_event="operator_max_retries_out_of_range",
            ),
            respect_robots=parse_bool_value(
                source.get("CRAWLUTRON_RESPECT_ROBOTS"),
                cls.respect_robots,
                env_name="CRAWLUTRON_RESPECT_ROBOTS",
            ),
            allowed_hosts=source.get("CRAWLUTRON_ALLOWED_HOSTS", cls.allowed_hosts),
            denied_hosts=source.get("CRAWLUTRON_DENIED_HOSTS", cls.denied_hosts),
            log_level=source.get("CRAWLUTRON_LOG_LEVEL", cls.log_level),
            log_file_path=source.get(ENVIRONMENT_FIELD_ALIASES["log_file_path"], cls.log_file_path),
            log_file_max_bytes=get_mapping_int(
                source,
                ENVIRONMENT_FIELD_ALIASES["log_file_max_bytes"],
                cls.log_file_max_bytes,
            ),
            log_file_backup_count=get_mapping_int(
                source,
                ENVIRONMENT_FIELD_ALIASES["log_file_backup_count"],
                cls.log_file_backup_count,
            ),
            cache_dir=source.get("CRAWLUTRON_CACHE_DIR", cls.cache_dir),
            cache_ttl_seconds=get_mapping_int(source, "CRAWLUTRON_CACHE_TTL_SECONDS", cls.cache_ttl_seconds),
            content_cache_enabled=parse_bool_value(
                source.get("CRAWLUTRON_CONTENT_CACHE_ENABLED"),
                cls.content_cache_enabled,
                env_name="CRAWLUTRON_CONTENT_CACHE_ENABLED",
            ),
            adaptive_throttling=parse_bool_value(
                source.get("CRAWLUTRON_ADAPTIVE_THROTTLING"),
                cls.adaptive_throttling,
                env_name="CRAWLUTRON_ADAPTIVE_THROTTLING",
            ),
            adaptive_min_delay=get_mapping_float(
                source, "CRAWLUTRON_ADAPTIVE_MIN_DELAY", cls.adaptive_min_delay
            ),
            adaptive_max_delay=get_mapping_float(
                source, "CRAWLUTRON_ADAPTIVE_MAX_DELAY", cls.adaptive_max_delay
            ),
            adaptive_latency_factor=get_mapping_float(
                source, "CRAWLUTRON_ADAPTIVE_LATENCY_FACTOR", cls.adaptive_latency_factor
            ),
            cookie_persistence_enabled=parse_bool_value(
                source.get("CRAWLUTRON_COOKIE_PERSISTENCE_ENABLED"),
                cls.cookie_persistence_enabled,
                env_name="CRAWLUTRON_COOKIE_PERSISTENCE_ENABLED",
            ),
            cookie_jar_path=source.get("CRAWLUTRON_COOKIE_JAR_PATH", cls.cookie_jar_path),
            http_max_connections=get_mapping_int(
                source, "CRAWLUTRON_HTTP_MAX_CONNECTIONS", _default_http_max_connections()
            ),
            http_max_keepalive=get_mapping_int(
                source, "CRAWLUTRON_HTTP_MAX_KEEPALIVE", _default_http_keepalive()
            ),
            http_keepalive_expiry=get_mapping_float(
                source, "CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY", cls.http_keepalive_expiry
            ),
            max_browser_renders=get_mapping_int(
                source, "CRAWLUTRON_MAX_BROWSER_RENDERS", cls.max_browser_renders
            ),
            max_concurrent_pages=get_mapping_int(
                source, "CRAWLUTRON_MAX_CONCURRENT_PAGES", cls.max_concurrent_pages
            ),
            browser_pool_size=get_mapping_int(source, "CRAWLUTRON_BROWSER_POOL_SIZE", cls.browser_pool_size),
            challenge_wait_seconds=get_mapping_int(
                source, "CRAWLUTRON_CHALLENGE_WAIT_SECONDS", cls.challenge_wait_seconds
            ),
            browser_lazy_scroll_steps=get_mapping_int(
                source,
                ENVIRONMENT_FIELD_ALIASES["browser_lazy_scroll_steps"],
                cls.browser_lazy_scroll_steps,
                lo=0,
            ),
            browser_lazy_scroll_delay_seconds=get_mapping_float(
                source,
                ENVIRONMENT_FIELD_ALIASES["browser_lazy_scroll_delay_seconds"],
                cls.browser_lazy_scroll_delay_seconds,
                lo=0.0,
            ),
            browser_cookie_harvest_timeout_seconds=get_mapping_float(
                source,
                ENVIRONMENT_FIELD_ALIASES["browser_cookie_harvest_timeout_seconds"],
                cls.browser_cookie_harvest_timeout_seconds,
                lo=0.0,
            ),
            extraction_doc_timeout=get_mapping_float(
                source, "CRAWLUTRON_EXTRACTION_DOC_TIMEOUT", cls.extraction_doc_timeout, lo=0.0
            ),
            extraction_selector_timeout=get_mapping_float(
                source, "CRAWLUTRON_EXTRACTION_SELECTOR_TIMEOUT", cls.extraction_selector_timeout, lo=0.0
            ),
            extraction_doc_acquire_timeout=get_mapping_float(
                source,
                "CRAWLUTRON_EXTRACTION_DOC_ACQUIRE_TIMEOUT",
                cls.extraction_doc_acquire_timeout,
                lo=0.0,
            ),
            extraction_max_markdown=get_mapping_int(
                source, "CRAWLUTRON_EXTRACTION_MAX_MARKDOWN", cls.extraction_max_markdown, lo=1
            ),
            search_provider=source.get("CRAWLUTRON_SEARCH_PROVIDER", cls.search_provider),
            tavily_api_key=source.get("CRAWLUTRON_TAVILY_API_KEY", cls.tavily_api_key),
            brave_api_key=source.get("CRAWLUTRON_BRAVE_API_KEY", cls.brave_api_key),
            serper_api_key=source.get("CRAWLUTRON_SERPER_API_KEY", cls.serper_api_key),
            search_limit_default=get_mapping_int(
                source, "CRAWLUTRON_SEARCH_LIMIT_DEFAULT", cls.search_limit_default
            ),
            search_limit_max=get_mapping_int(source, "CRAWLUTRON_SEARCH_LIMIT_MAX", cls.search_limit_max),
            search_timeout_seconds=get_mapping_int(
                source, "CRAWLUTRON_SEARCH_TIMEOUT_SECONDS", cls.search_timeout_seconds
            ),
        )
