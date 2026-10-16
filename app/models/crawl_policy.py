from __future__ import annotations

from typing import Annotated, Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.models.crawl_policy_vocabulary import (
    MAX_HEADERS_PER_REQUEST,
    REQUEST_PROFILE_KEYS_JSON,
    close_browser_profile_vocabulary,
    close_request_profile_vocabulary,
    narrows_credential_scope,
)
from app.models.crawl_strategy import CrawlStrategy
from app.models.credential import Credential
from app.models.discovery_config import DiscoveryConfig

POLICY_SCHEMA_VERSION = "2026-06-23"


MAX_CRAWL_STRATEGIES: Final[int] = 16


__all__ = ["CrawlPolicy", "MAX_CRAWL_STRATEGIES", "POLICY_SCHEMA_VERSION"]


class CrawlPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # See the module preamble: why each name is rejected, in three kinds.
    _BANNED_HEADERS: ClassVar[dict[str, str]] = {
        "if-none-match": "conditional headers belong to the validators map",
        "if-modified-since": "conditional headers belong to the validators map",
        "cookie": "the cookie jar owns Cookie, and request_profile.cookies is its sanctioned second door; "
        "a raw header bypasses both",
        "host": "Host is owned by the pinned-request chokepoint and is never forwarded verbatim",
        "proxy-authorization": "proxy credentials are not a crawl input",
        "x-forwarded-for": "origin-side identity headers must not be caller-controlled",
        "x-forwarded-host": "origin-side identity headers must not be caller-controlled",
        "x-real-ip": "origin-side identity headers must not be caller-controlled",
        "content-length": "body framing belongs to the HTTP client, not to a crawl input",
        "transfer-encoding": "body framing belongs to the HTTP client, not to a crawl input",
        "connection": "hop-by-hop header; the connection is not the caller's to describe",
        "te": "hop-by-hop header; transfer coding is negotiated per connection",
        "trailer": "hop-by-hop header; trailers are framed by the client, not declared here",
        "upgrade": "hop-by-hop header; protocol upgrade is not a crawl input",
        "keep-alive": "hop-by-hop header; connection persistence is the client's decision",
    }

    schema_version: Annotated[
        str,
        Field(
            description="Caller-resolved policy schema version. Use the current policy_schema_version from "
            "/v1/capabilities or /v1/agent-hints when constructing durable policy templates.",
        ),
    ] = POLICY_SCHEMA_VERSION
    strategies: list[CrawlStrategy] = Field(
        default_factory=lambda: [
            CrawlStrategy.HTTP_DECLARED_CRAWLER,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
            CrawlStrategy.BROWSER_HEADLESS,
            CrawlStrategy.BROWSER_HEADED_WARMED,
        ],
        min_length=1,
        max_length=MAX_CRAWL_STRATEGIES,
        description="Ordered fetch execution chain. Default: HTTP_DECLARED_CRAWLER, then "
        "HTTP_CFFI_IMPERSONATE, then BROWSER_HEADLESS, then BROWSER_HEADED_WARMED. Strategies run in order, "
        "the service records every attempted strategy, and fallback stops at the first successful content "
        "result. Available fetch strategies: HTTP_DECLARED_CRAWLER (transparent direct HTTP with the "
        "service's declared crawler identity), HTTP_CFFI_IMPERSONATE (TLS-fingerprint-impersonated HTTP via "
        "curl_cffi with browser-shaped headers, for sites that reject non-browser clients), BROWSER_HEADLESS "
        "(headless Firefox rendering via Camoufox), BROWSER_HEADED_WARMED (headed Firefox with warmup URLs).",
    )
    extraction: Annotated[
        CrawlExtractionMethod,
        Field(
            description="Output/parsing pipeline applied to the content the selected fetch strategy "
            "retrieves. Orthogonal to the fetch strategy. FULL (default) uses the unified pipeline: "
            "trafilatura HTML main text, markitdown for documents, plain-text passthrough. TRAFILATURA "
            "forces HTML main-text extraction only. DOCUMENT forces markitdown document extraction for PDFs "
            "and other non-HTML bodies. These are not strategies; pair any of them with any fetch strategy.",
        ),
    ] = CrawlExtractionMethod.FULL
    limits: CrawlPolicyLimits = Field(
        default_factory=CrawlPolicyLimits,
        description="Runtime limits applied to the execution plan. Omit to use service defaults from "
        "/v1/capabilities; set per request when a host needs a tighter timeout, retry budget, passive "
        "challenge wait, or discovered-link cap.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=MAX_HEADERS_PER_REQUEST,
        description="Optional caller-approved headers for direct HTTP strategies. Use for explicit, "
        "reviewable host policies; do not use to smuggle credentials or bypass access controls. "
        "Authorization belongs in policy.credentials, which scopes it to one origin.",
    )
    credentials: dict[str, Credential] = Field(
        default_factory=dict,
        max_length=32,
        description="Optional per-origin credentials for credentialed crawling of the caller's own sites, "
        "keyed by canonical origin (scheme://host[:port], no path). Applied as the Authorization header only "
        "for requests to that exact origin, and stripped by the redirect chokepoint on any cross-host hop.",
    )
    request_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional direct HTTP compatibility profile for HTTP_CFFI_IMPERSONATE. Supported keys: "
        "user_agent_family (for example desktop_chrome), user_agents, accept_languages, "
        "fingerprint_seed, fingerprint_rotation, cookie_jar, and cookies for public sessions. Applied only "
        "to direct HTTP strategies and echoed in diagnostics.",
        json_schema_extra=REQUEST_PROFILE_KEYS_JSON,
    )
    browser_profile: dict[str, Any] = Field(
        default_factory=dict,
        description="Browser interaction profile for browser strategies. Supported keys: viewport "
        "(desktop or mobile), resource_mode (full or reduced), storage_state, actions, include_html, "
        "include_screenshot, screenshot_selectors, auto_accept_cookie_consent (default false; when "
        "true the browser may click consent banners), lazy_scroll_steps, lazy_scroll_delay_seconds, "
        "and cookie_harvest_timeout_seconds. "
        "Viewport changes Firefox window size; reduced resource mode disables images.",
    )
    discovery: Annotated[
        DiscoveryConfig | None,
        Field(
            description="Optional discovery toggles. Set robots, sitemaps, or feeds true to enable that "
            "source; omitted or null leaves the stored discovery map empty.",
        ),
    ] = None
    scope_regex: Annotated[
        str | None,
        Field(
            description="Optional regex applied to each canonical discovered-link identity before recursive "
            "frontier admission. Non-matching links may still be reported in discovered_links but are not "
            "followed by /v1/crawl or /v1/jobs.",
        ),
    ] = None
    css_selector: Annotated[
        str | None,
        Field(
            description="Optional CSS selector. When provided, extraction targets only the matched DOM "
            "subtree (BeautifulSoup) and returns it as Markdown, bypassing full-page "
            "Trafilatura/markdownify.",
        ),
    ] = None
    xpath: Annotated[
        str | None,
        Field(
            description="Optional XPath expression. When provided, extraction targets only the matched DOM "
            "nodes (lxml) and returns them as Markdown, bypassing full-page Trafilatura/markdownify.",
        ),
    ] = None

    @field_validator("scope_regex")
    @classmethod
    def _validate_scope_regex(cls, value: str | None) -> str | None:
        from app.safety.safe_regex import UnsafePattern, compile_untrusted

        if value is None:
            return None
        try:
            compile_untrusted(value, max_len=256)
        except UnsafePattern as exc:
            raise ValueError(f"scope_regex rejected: {exc}") from exc
        return value

    @field_validator("browser_profile")
    @classmethod
    def _close_the_browser_profile_vocabulary(cls, profile: dict[str, Any]) -> dict[str, Any]:
        return close_browser_profile_vocabulary(profile)

    @field_validator("request_profile")
    @classmethod
    def _close_the_request_profile_vocabulary(cls, profile: dict[str, Any]) -> dict[str, Any]:
        return close_request_profile_vocabulary(profile)

    @field_validator("credentials", mode="before")
    @classmethod
    def _canonicalize_credential_origins(cls, value: Any) -> Any:
        """Key every credential by its canonical origin, at the read edge; the rule lives
        in ``app.admission.credentials.origin_of`` (the same function every lookup uses), so one
        rule at one site. A path-scoped or duplicate-canonical key is rejected, never narrowed.
        """
        if not isinstance(value, dict):
            return value
        from app.admission.credentials import origin_of

        canonical: dict[str, Any] = {}
        spelling: dict[str, str] = {}
        for key, credential in value.items():
            if not isinstance(key, str) or narrows_credential_scope(key):
                raise ValueError("credentials key must be a bare origin: scheme://host[:port]")
            origin = origin_of(key)
            if origin in canonical:
                raise ValueError(
                    f"credentials keys {spelling[origin]!r} and {key!r} are both origin "
                    f"{origin!r}; one would silently replace the other"
                )
            canonical[origin] = credential
            spelling[origin] = key
        return canonical

    @field_validator("headers")
    @classmethod
    def _reject_banned_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject chokepoint-bypassing header names, anchored on ``CrawlPolicy`` so every
        endpoint inherits the ban from one site -- rejection, never a silent strip; ``authorization``
        is deliberately absent (``policy.credentials`` scopes it). A field validator, so the location maps.
        """
        for key in value:
            reason = cls._BANNED_HEADERS.get(key.strip().lower())
            if reason is not None:
                raise ValueError(f"header '{key}' is not permitted in policy.headers: {reason}")
        return value
