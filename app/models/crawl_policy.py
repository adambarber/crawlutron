from __future__ import annotations

from typing import Annotated, Any, ClassVar, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.models.crawl_strategy import CrawlStrategy
from app.models.credential import Credential
from app.models.discovery_config import DiscoveryConfig
from app.vocabulary.request_profiles import REQUEST_PROFILE_KEYS

POLICY_SCHEMA_VERSION = "2026-06-23"

MAX_HEADERS_PER_REQUEST = 64

MAX_BROWSER_ACTIONS = 64

MAX_CRAWL_STRATEGIES: Final[int] = 16

_REQUEST_PROFILE_KEYS_JSON: dict[str, Any] = {"keys": sorted(REQUEST_PROFILE_KEYS)}


def _close_browser_profile_vocabulary(profile: dict[str, Any]) -> dict[str, Any]:
    """Refuse inert keys and inert actions in ``browser_profile``, and say what is allowed.

    This model was ``dict[str, Any]`` with exactly one key checked
    (``execute_js``). Everything else a caller could set was prose in a field
    description, which is not enforcement: a misspelled action type passed the
    boundary and was then silently skipped by the renderer's if/elif chain, so
    the caller got a page that loaded with nothing done and nothing said. An
    inert key and a key that worked looked identical.

    The vocabulary is imported from ``app.vocabulary`` rather than restated,
    because the renderer's chain recognises the same set and the two lists have
    to agree -- the shape of bug this closes. ``execute_js`` stays refused in
    BOTH places on purpose: here so a malicious payload never reaches a render,
    and in the renderer so a policy arriving by any other route still cannot run
    script.

    Action types are compared case-insensitively because the renderer lowercases
    before matching. Refusing ``CLICK`` while permitting ``click`` would refuse
    something the consumer handles.

    Refusals are ``ValueError`` on purpose, including for the two shape errors
    below. Pydantic wraps a ``ValueError`` into a 422 but lets a ``TypeError``
    escape the request handler entirely, so the first version of this function --
    which iterated ``profile["actions"]`` directly -- turned ``{"actions": 5}``
    into an HTTP 500 on every policy-bearing endpoint. A caller mistake must
    arrive as a refusal, which is the only shape the caller can act on.
    """
    from app.vocabulary.browser import ALLOWED_BROWSER_ACTION_TYPES, BROWSER_PROFILE_KEYS

    if not isinstance(profile, dict):
        return profile
    allowed = ", ".join(sorted(ALLOWED_BROWSER_ACTION_TYPES))

    unknown_keys = sorted(set(profile) - BROWSER_PROFILE_KEYS)
    if unknown_keys:
        raise ValueError(
            f"browser_profile keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(BROWSER_PROFILE_KEYS))}"
        )

    _refuse_over_ceiling_knobs(profile)

    actions = profile.get("actions")
    if actions is None:
        return profile
    if not isinstance(actions, list):
        raise ValueError(
            f"browser_profile actions must be a list of action objects, got {type(actions).__name__}"
        )
    if len(actions) > MAX_BROWSER_ACTIONS:
        raise ValueError(
            f"browser_profile actions may carry at most {MAX_BROWSER_ACTIONS} entries, got {len(actions)}"
        )

    for index, action in enumerate(actions):
        _refuse_unusable_action(action, index, allowed)
    return profile


def _refuse_over_ceiling_knobs(profile: dict[str, Any]) -> None:
    """Refuse a closed key whose value is larger than the work it buys is worth.

    The vocabulary is closed, which is a claim about which keys exist and nothing
    about how large their values may be. Four knobs took whatever the caller
    sent, each in a place the code already said should be bounded: the lazy scroll
    runs directly below an action chain that *is* budgeted and whose docstring
    says production must pass a budget; the cookie harvest holds a pooled
    renderer from a finally block; and every screenshot selector appends a
    base64 PNG to the result, so that one bounds the response body too.

    The readers clamp the same values (see ``_profile_int``), because by the time
    a profile reaches a renderer it is a dict and the validator is not in the
    path. This refusal exists so the caller is *told*, rather than silently
    handed a render that finished early.
    """
    from app.vocabulary.limits import (
        MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
        MAX_LAZY_SCROLL_DELAY_SECONDS,
        MAX_LAZY_SCROLL_STEPS,
        MAX_SCREENSHOT_SELECTORS,
    )

    for key, ceiling in (
        ("lazy_scroll_steps", MAX_LAZY_SCROLL_STEPS),
        ("lazy_scroll_delay_seconds", MAX_LAZY_SCROLL_DELAY_SECONDS),
        ("cookie_harvest_timeout_seconds", MAX_COOKIE_HARVEST_TIMEOUT_SECONDS),
    ):
        value = profile.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > ceiling:
            raise ValueError(f"browser_profile {key} may be at most {ceiling}, got {value}")

    selectors = profile.get("screenshot_selectors")
    if isinstance(selectors, list) and len(selectors) > MAX_SCREENSHOT_SELECTORS:
        raise ValueError(
            f"browser_profile screenshot_selectors may carry at most {MAX_SCREENSHOT_SELECTORS} "
            f"entries, got {len(selectors)}"
        )


def _refuse_unusable_action(action: Any, index: int, allowed: str) -> None:
    """Refuse one action the renderer would accept and then do nothing about.

    Split out of :func:`_close_browser_profile_vocabulary` so the three
    refusals read as the three distinct failures they are, rather than as one
    long loop body. ``allowed`` is the already-formatted list of permitted types,
    passed in so this stays a check on one action rather than a place the
    vocabulary is read a second time.
    """
    from app.vocabulary.browser import (
        ACTIONS_REQUIRING_SELECTOR,
        ALLOWED_BROWSER_ACTION_TYPES,
        BROWSER_ACTION_KEYS,
    )

    if not isinstance(action, dict):
        raise ValueError("each browser_profile action must be an object with a 'type'")

    unknown_keys = sorted(set(action) - BROWSER_ACTION_KEYS)
    if unknown_keys:
        raise ValueError(
            f"browser_profile action keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(BROWSER_ACTION_KEYS))}"
        )

    action_type = str(action.get("type", "")).lower()
    if action_type == "execute_js":
        raise ValueError(
            "execute_js actions are not permitted in browser_profile: this service does not "
            "run caller-supplied script in the browser context"
        )
    if action_type not in ALLOWED_BROWSER_ACTION_TYPES:
        raise ValueError(
            f"browser_profile action type {action_type!r} is not permitted; allowed types are: {allowed}"
        )
    # The chain cannot report this one. Its branch reads
    # ``if action_type == "click" and selector:``, so a selector-less click
    # is not an error there, it is a no-op, and the request returns 200 with
    # a page that loaded and nothing done. Refusing here is the only place the
    # caller learns why. ``selector: ""`` counts as absent for the same reason:
    # it is falsy at the branch, so it behaves identically once it gets there.
    if action_type in ACTIONS_REQUIRING_SELECTOR and not action.get("selector"):
        raise ValueError(
            f"browser_profile action at index {index} is of type {action_type!r}, which acts "
            "on a named element, so it must carry a non-empty 'selector'"
        )


def _close_request_profile_vocabulary(profile: dict[str, Any]) -> dict[str, Any]:
    """Refuse inert keys and malformed values in ``request_profile``, and say what is allowed.

    This model was ``dict[str, Any]`` with no constraints at all, while its
    siblings on the same class are closed: ``headers`` refuses a banned name and
    ``credentials`` refuses a colliding origin. Two consequences, both silent:

    - A misspelled key is accepted and does nothing. ``{"cookie_jarr":
      "per_host"}`` disables the per-host jar; ``{"fingerprint_rotatoin": True}``
      disables fingerprint rotation. Both report success, which is the same
      failure as the inert ``state`` key this series removed from
      ``browser_profile``, and the same one the field description was already
      committing by advertising ``accept_language`` when the code reads
      ``accept_languages``.
    - A malformed *value* is accepted and breaks deeper in, where it cannot be
      reported. The consumer is annotated ``-> dict[str, str]``, so a pool entry
      that is a mapping puts a ``dict`` in ``User-Agent``; and
      ``{"user_agents": [None]}`` raises ``TypeError`` from inside the header
      builder, which is an HTTP 500. The same validator-shape bug that turned
      ``{"actions": 5}`` into a 500 in the browser profile, one module over.

    ``cookies`` is a second door to the ``Cookie`` header, which is why its size
    and shape are bounded here. ``headers`` refuses a raw ``Cookie`` on the
    grounds that "the cookie jar owns Cookie; a raw header bypasses the
    cookie-namespace chokepoint", and a caller-supplied nested dict assembled
    into that same header is the same capability reached by a door the ban does
    not inspect. What that means for *isolation* is not this validator's call to
    make: a ``cookie_namespace`` is a string the caller picks, with no
    authentication behind it, so it is a tenant key and not a boundary. The ban's
    rationale and ``cookie_jar``'s isolation claim are corrected in #64 to say
    so; here the ceiling is the part that is unambiguous.

    Every refusal here is ``ValueError`` on purpose. Pydantic wraps a
    ``ValueError`` into a 422 and lets a ``TypeError`` past the request handler,
    so a caller mistake must arrive as a refusal, which is the only shape the
    caller can act on.
    """
    if not isinstance(profile, dict):
        return profile

    unknown_keys = sorted(set(profile) - REQUEST_PROFILE_KEYS)
    if unknown_keys:
        raise ValueError(
            f"request_profile keys not permitted: {', '.join(unknown_keys)}; "
            f"supported keys are: {', '.join(sorted(REQUEST_PROFILE_KEYS))}"
        )

    _refuse_malformed_profile_values(profile)
    return profile


def _refuse_malformed_profile_values(profile: dict[str, Any]) -> None:
    """Refuse a value of the wrong shape behind a permitted key.

    Split from the vocabulary check because the two answer different questions:
    the caller asked for a key that exists, or the caller asked for something that
    is not the thing that key means. The second was the one that had no home, so
    a malformed value reached the header builder and raised from inside it.
    """
    from app.vocabulary.request_profiles import (
        MAX_COOKIE_NAMESPACE_LENGTH,
        MAX_PROFILE_COOKIE_HOSTS,
        MAX_PROFILE_COOKIE_PAIRS,
        MAX_PROFILE_POOL_ENTRIES,
        REQUEST_PROFILE_FLAG_KEYS,
        REQUEST_PROFILE_POOL_KEYS,
        REQUEST_PROFILE_TEXT_KEYS,
    )

    for key, value in profile.items():
        if key in REQUEST_PROFILE_FLAG_KEYS and not isinstance(value, bool):
            raise ValueError(f"request_profile {key} must be true or false, got {type(value).__name__}")
        if key in REQUEST_PROFILE_TEXT_KEYS and not isinstance(value, str):
            raise ValueError(f"request_profile {key} must be a string, got {type(value).__name__}")

    namespace = profile.get("cookie_namespace")
    if isinstance(namespace, str) and len(namespace) > MAX_COOKIE_NAMESPACE_LENGTH:
        raise ValueError(
            f"request_profile cookie_namespace may carry at most {MAX_COOKIE_NAMESPACE_LENGTH} "
            f"characters, got {len(namespace)}"
        )

    for key in sorted(REQUEST_PROFILE_POOL_KEYS & profile.keys()):
        _refuse_malformed_pool(profile, key, MAX_PROFILE_POOL_ENTRIES)

    if "cookies" in profile:
        _refuse_malformed_cookie_map(profile, MAX_PROFILE_COOKIE_HOSTS, MAX_PROFILE_COOKIE_PAIRS)


def _refuse_malformed_pool(profile: dict[str, Any], key: str, ceiling: int) -> None:
    """Refuse a header pool that is not a bounded list of strings.

    Split out so the two refusals read as the two failures they are: the wrong
    container, and the wrong thing inside the right container. A ``[None]`` here
    used to reach the header builder and raise from inside it.
    """
    pool = profile[key]
    if not isinstance(pool, list):
        raise ValueError(f"request_profile {key} must be a list of strings, got {type(pool).__name__}")
    if len(pool) > ceiling:
        raise ValueError(f"request_profile {key} may carry at most {ceiling} entries, got {len(pool)}")
    for entry in pool:
        if not isinstance(entry, str):
            raise ValueError(f"request_profile {key} entries must be strings, got {type(entry).__name__}")


def _refuse_malformed_cookie_map(profile: dict[str, Any], host_ceiling: int, pair_ceiling: int) -> None:
    """Refuse a static cookie map that is unbounded or malformed.

    Without these a 200,000-host map parsed into one policy, and because
    ``content_cache_key`` serialises the whole header map into the cache key,
    every distinct value became a distinct cache slot holding a full response
    body.

    Note what this deliberately does NOT refuse: a cookie map with no
    ``cookie_namespace``. That is the documented single-tenant path, and
    ``cookie_jar``'s docstring commits to preserving it. A namespace is a tenant
    key chosen by the caller, and this service has no caller authentication, so
    it cannot be an authorization boundary -- see #64.
    """
    cookies = profile["cookies"]
    if not isinstance(cookies, dict):
        raise ValueError(
            f"request_profile cookies must be a mapping of host to cookies, got {type(cookies).__name__}"
        )
    if len(cookies) > host_ceiling:
        raise ValueError(f"request_profile cookies may name at most {host_ceiling} hosts, got {len(cookies)}")
    for host, pairs in cookies.items():
        if not isinstance(pairs, dict):
            raise ValueError(
                f"request_profile cookies for host {host!r} must be a mapping of name to value, "
                f"got {type(pairs).__name__}"
            )
        if len(pairs) > pair_ceiling:
            raise ValueError(
                f"request_profile cookies for host {host!r} may carry at most {pair_ceiling} "
                f"entries, got {len(pairs)}"
            )
        for name, value in pairs.items():
            if not isinstance(name, str) or not isinstance(value, (str, int, float)):
                raise ValueError(
                    f"request_profile cookie {name!r} for host {host!r} must be a string name and a "
                    "string or number value"
                )


def _narrows_credential_scope(key: str) -> bool:
    """True when a credential key names less than a whole origin.

    A key that cannot reach every path on its host is a narrower scope than
    the per-origin lookup it would be normalized into, so the two are not two
    spellings of one thing. The empty path is not narrower: it is how
    ``scheme://host`` is written.
    """
    parsed = urlsplit(key.strip())
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        return True
    return parsed.path not in ("", "/")


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
        json_schema_extra=_REQUEST_PROFILE_KEYS_JSON,
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
        from app.safe_regex import UnsafePattern, compile_untrusted

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
        return _close_browser_profile_vocabulary(profile)

    @field_validator("request_profile")
    @classmethod
    def _close_the_request_profile_vocabulary(cls, profile: dict[str, Any]) -> dict[str, Any]:
        return _close_request_profile_vocabulary(profile)

    @field_validator("credentials", mode="before")
    @classmethod
    def _canonicalize_credential_origins(cls, value: Any) -> Any:
        """Key every credential by its canonical origin, at the read edge.

                The lookup at request time is a plain dict hit on
                ``app.admission.credentials.origin_of(url)``, which lowercases the scheme
                and host and drops a default port. The canonicality rule itself lives
                in ``origin_of`` -- the same function every lookup uses, so one rule at
                one site -- and this is the only place it is applied to a stored key.

                Two spellings of one origin are canonicalized; a key that is not an
                origin is REJECTED rather than narrowed. ``https://a.example/secret``
                is a path-scoped key, and canonicalizing it to ``https://a.example``
                would silently widen the credential from one path to every request to
                that host, which is the outcome this rejection exists to prevent. So only the
                parts that cannot change what the credential reaches are normalized --
                scheme and host case, and a default port -- and a key that narrows the
                scope is reported to the caller rather than rewritten into a broader
                one.

                The distinction matters because rejecting both was a real failure in
                the other direction: a caller who wrote ``HTTPS://Private.Example:443/``
                is naming the origin they meant, and rejection left them with a
                credential that validated, appeared in their policy, and then never
                applied to the request it was for. Nothing said so at either end.
        Two keys that canonicalize to the SAME origin are rejected, for the same
                reason a path-scoped key is: the comprehension that builds the canonical
                map keeps the last and drops the first, so the caller ends up holding a
                policy that lists two credentials and has one, and nothing said so at
                either end. A credential the caller believes is applied is worse than one
                rejected here -- the request would simply go out unauthenticated.

                The error names both spellings, because "duplicate origin" alone leaves
                the caller to work out which two of their own keys collided.

                Each key is canonicalized ONCE, into the map that is returned, so the form
                a key is checked against is by construction the form it is stored under;
                there is no second computation that could disagree with the first.
        """
        if not isinstance(value, dict):
            return value
        from app.admission.credentials import origin_of

        canonical: dict[str, Any] = {}
        spelling: dict[str, str] = {}
        for key, credential in value.items():
            if not isinstance(key, str) or _narrows_credential_scope(key):
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
        """Reject chokepoint-bypassing header names in ``policy.headers``.

        Anchored on ``CrawlPolicy`` (not a request model) so every endpoint
        that embeds a policy -- crawl, map, render alike -- inherits the ban
        from one site; a per-request validator is a per-call-site guard the
        engine's chokepoints would still have to re-trust. Rejection, never a
        silent strip, so the caller learns the contract.

        ``authorization`` is deliberately absent: credentialed crawling of
        one's own site is a real use case, and the typed replacement
        (``policy.credentials``) scopes it per origin. Callers may
        still set a raw ``Authorization`` header for one-off needs, but the
        credentials field is the supported path: origin-scoped, scrubbed on
        cross-host hops by the same redirect scrub as cookies.

        A field validator rather than a model validator so the rejection
        carries a location. From a ``mode="after"`` hook pydantic reports
        ``loc=()``, which the API's code derivation cannot map, so a caller
        whose only mistake was a ``Cookie`` header was told ``invalid_policy``
        -- the one code that names no field -- instead of the
        ``invalid_policy_headers`` the contract documents. The check is the
        same single site either way; only the reported location changes.
        """
        for key in value:
            reason = cls._BANNED_HEADERS.get(key.strip().lower())
            if reason is not None:
                raise ValueError(f"header '{key}' is not permitted in policy.headers: {reason}")
        return value
