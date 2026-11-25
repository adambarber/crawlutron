"""Typed return shapes for the settings field readers.

Declared here so ``settings.py`` stays under the file ceiling; the reader
functions themselves stay with the defaults and aliases they consume.
"""

from __future__ import annotations

from typing import TypedDict


class CoreFields(TypedDict):
    """The keys `_core_fields` reads; the constructor consumes them by name."""

    host: str
    global_concurrency: int
    per_domain_concurrency: int
    per_domain_delay_ms: int
    max_batch_size: int
    default_timeout_seconds: int
    default_max_retries: int


class PolicyFields(TypedDict):
    """The keys `_policy_fields` reads: robots, hosts, and the log sink."""

    respect_robots: bool
    allowed_hosts: str
    denied_hosts: str
    log_level: str
    log_file_path: str
    log_file_max_bytes: int
    log_file_backup_count: int


class PersistenceFields(TypedDict):
    """The keys `_persistence_fields` reads: caches and adaptive throttling."""

    cache_dir: str
    cache_ttl_seconds: int
    content_cache_enabled: bool
    adaptive_throttling: bool
    adaptive_min_delay: float
    adaptive_max_delay: float
    adaptive_latency_factor: float
    cookie_persistence_enabled: bool
    cookie_jar_path: str


class TransportFields(TypedDict):
    """The keys `_transport_fields` reads: the HTTP connection pool."""

    http_max_connections: int
    http_max_keepalive: int
    http_keepalive_expiry: float


class BrowserFields(TypedDict):
    """The keys `_browser_fields` reads: render caps and fixed overheads."""

    max_browser_renders: int
    max_concurrent_pages: int
    challenge_wait_seconds: int
    browser_lazy_scroll_steps: int
    browser_lazy_scroll_delay_seconds: float
    browser_cookie_harvest_timeout_seconds: float


class ExtractionFields(TypedDict):
    """The keys `_extraction_fields` reads: extraction deadlines and caps."""

    extraction_doc_timeout: float
    extraction_selector_timeout: float
    extraction_doc_acquire_timeout: float
    extraction_max_markdown: int


class SearchFields(TypedDict):
    """The keys `_search_fields` reads: search providers and key admin."""

    search_provider: str
    tavily_api_key: str
    brave_api_key: str
    serper_api_key: str
    search_limit_default: int
    search_limit_max: int
    search_timeout_seconds: int
    key_admin_token: str
    webhook_secret: str
    max_watches_per_caller: int


class RetentionFields(TypedDict):
    """The keys `_retention_fields` reads: the retention sweep grace."""

    retention_sweep_grace_seconds: int


class OpsFields(TypedDict):
    """The keys `_ops_fields` reads: the four formerly-direct operator knobs."""

    max_webhook_size: int
    user_agent: str
    headless: bool
    warmup_urls: tuple[str, ...]
