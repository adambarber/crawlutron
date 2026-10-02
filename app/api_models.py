from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import (
    # When pyright handles pydantic v2's Field/validator overloads without help.
    BROWSER_HEADED_WARMED,
    BROWSER_HEADLESS,
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_DOM,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
    MAX_CONTENT_LENGTH_BYTES,
    MAX_DISCOVERED_LINKS,
    MAX_HTTP_TIMEOUT_SECONDS,
    MAX_JOB_MAX_RETRIES,
    MAX_RETRIES,
    MAX_TOTAL_URL_BUDGET_SECONDS,
    REQUEST_PROFILE_KEYS,
    SERVICE_ID,
    SERVICE_VERSION,
)

#: The output formats a request may name, imported from the module that has to
#: dispatch on them rather than restated. The field description ("One of: json
#: (default), jsonl, ndjson, markdown, csv"), this annotation, and
#: ``SUPPORTED_OUTPUT_FORMATS`` are one decision with one home:
#: ``app.output_formats``. This module used to spell its own ``Literal`` beside
#: that set and comment that it was the only copy, which was true of neither the
#: code nor the tests.
from app.output_formats import OutputFormat

# `plugins = pydantic.mypy` in mypy.ini is what lets the declared fail gate read
# these models; without it every pydantic import resolves as untyped and the gate
# is blind to all of them.
#
# This comment used to carry the opposite claim -- that pydantic's
# `Field(default_factory=...)` and `@model_validator(mode="after")` overloads
# defeat pyright, and that six `reportArgumentType` findings here were each
# suppressed per line with that reason. They suppressed nothing.
# `reportUnnecessaryTypeIgnoreComment` (now on in pyrightconfig.json) reports
# every one of them as unnecessary, and a worktree at 7d0aed1 -- the commit that
# added them -- reports zero `reportArgumentType` findings in this file. The
# number in the old comment did not match the number of suppressions either.

POLICY_SCHEMA_VERSION = "2026-06-23"

#: Ceilings on caller-supplied collections. Declared here, once, because they are
#: policy rather than schema: three of these four fields are dicts or lists a
#: caller can size freely, and an unbounded one is a fan-out the service agreed
#: to before any admission check runs. ``credentials`` already carried 32 and
#: keeps it -- the point is that its siblings now carry a decision too, not that
#: they match it.
MAX_URLS_PER_REQUEST = 500
MAX_VALIDATORS_PER_REQUEST = 64
MAX_HEADERS_PER_REQUEST = 64
#: The key allowlist bounds how many keys a ``browser_profile`` may carry, not how
#: big their values are. Execution is already bounded by the render budget, so an
#: oversized action list costs parse time and memory rather than unbounded work --
#: but it is the same shape as the unbounded ``urls`` collection above, and a
#: caller should not be able to hand the model 200,000 dicts.
MAX_BROWSER_ACTIONS = 64

# The identity block every discovery response opens with, declared once. It lives
# here rather than in ``app.constants`` because it spans both: the version comes
# from constants, the policy schema version is this module's own. Two payloads
# were spelling the same three keys, and that is exactly the shape of bug this
# codebase has already shipped once -- a top-level key reaching one endpoint and
# not the other, which a strict response_model DROPS without complaint, so a
# raw-dict unit test stays green while the wire loses the field. Spreading one
# block means a fourth key is added in one place and cannot reach only some of
# the endpoints.
SERVICE_IDENTITY: Final[dict[str, str]] = {
    "service": SERVICE_ID,
    "version": SERVICE_VERSION,
    "policy_schema_version": POLICY_SCHEMA_VERSION,
}


class CrawlStrategy(StrEnum):
    """Fetch execution strategies, tried in fallback order.

    These describe *how* to retrieve bytes for a URL. How those bytes are
    parsed into text/markdown is a separate concern handled by
    :class:`CrawlExtractionMethod` (the ``extraction`` policy field), which is
    orthogonal to the fetch strategy.
    """

    HTTP_DECLARED_CRAWLER = HTTP_DECLARED_CRAWLER
    HTTP_CFFI_IMPERSONATE = HTTP_CFFI_IMPERSONATE
    BROWSER_HEADLESS = BROWSER_HEADLESS
    BROWSER_HEADED_WARMED = BROWSER_HEADED_WARMED


class CrawlExtractionMethod(StrEnum):
    """Output/parsing pipeline applied to content a fetch strategy retrieves.

    Extraction is orthogonal to the fetch strategy: any fetch strategy may be
    paired with any extraction method. ``FULL`` is the default unified
    pipeline (trafilatura HTML main text, markitdown for documents, plain-text
    passthrough). ``TRAFILATURA`` forces HTML main-text extraction only.
    ``DOCUMENT`` forces markitdown document extraction for PDFs and other
    non-HTML bodies.
    """

    FULL = EXTRACTION_FULL
    TRAFILATURA = EXTRACTION_TRAFILATURA
    DOCUMENT = EXTRACTION_DOCUMENT
    DOM = EXTRACTION_DOM


class CrawlStatus(StrEnum):
    success = "success"
    blocked = "blocked"
    disallowed = "disallowed"
    retry_exhausted = "retry_exhausted"
    failed = "failed"
    skipped = "skipped"


class DiscoveryConfig(BaseModel):
    """Typed discovery toggles for crawl policy.

    Only recognized keys are accepted; extra keys are rejected.

    Every channel defaults to off, which is what "set robots, sitemaps, or feeds
    true to enable that source" in each field's own description says. These
    defaulted to on, and nothing noticed for two reasons: a consumer that wanted
    one channel read the others with a defaulted lookup over the raw mapping, so
    an absent key and an explicit ``false`` were the same answer; and the block
    is optional, so a policy that never mentioned discovery reached the consumer
    as an empty mapping rather than as this model. Making the model
    authoritative removed both hiding places and turned ``{"discovery": {}}``
    from "no discovery" into "fetch robots, sitemaps and feeds" -- the opposite
    of what a caller asking for nothing gets. Off is also the cheaper default to
    be wrong about in the safe direction, since every channel here costs a
    request.
    """

    robots: Annotated[
        bool,
        Field(
            description="Honor /robots.txt rules and report robots crawl-delay, sitemaps, and disallow "
            "patterns.",
        ),
    ] = False
    sitemaps: Annotated[
        bool,
        Field(
            description="Fetch and parse sitemap.xml / sitemap_index.xml URLs from robots.txt and common "
            "locations.",
        ),
    ] = False
    feeds: Annotated[
        bool,
        Field(
            description='Detect RSS/Atom/JSON Feed links via <link rel="alternate"> and common feed paths.',
        ),
    ] = False

    model_config = ConfigDict(extra="forbid")


class FetchValidators(BaseModel):
    """Caller-supplied cached validators for cheap conditional revalidation.

    Supply the ETag / Last-Modified you received from a previous crawl of the
    same URL; matching URLs are revalidated with If-None-Match /
    If-Modified-Since instead of fully re-fetched. A 304 yields a
    ``not_modified`` result (no content) so freshness monitors avoid paying
    full-fetch cost on unchanged pages.
    """

    model_config = ConfigDict(extra="forbid")

    if_none_match: Annotated[
        str | None,
        Field(
            description="ETag from a previous crawl result; sent as If-None-Match.",
        ),
    ] = None
    if_modified_since: Annotated[
        str | None,
        Field(
            description="Last-Modified date from a previous crawl result; sent as If-Modified-Since.",
        ),
    ] = None


class ContentValidators(BaseModel):
    """Validators returned by the origin for the delivered representation.

    Feed ``etag`` / ``last_modified`` back as ``if_none_match`` /
    ``if_modified_since`` on a later request to revalidate cheaply.
    """

    etag: Annotated[
        str | None,
        Field(
            description="Origin ETag for the final representation, if provided.",
        ),
    ] = None
    last_modified: Annotated[str | None, Field(description="Origin Last-Modified date, if provided.")] = None


class CrawlPolicyLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_seconds: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_HTTP_TIMEOUT_SECONDS,
            description="Per-strategy timeout budget in seconds. Default: 30. Applies separately to each "
            "attempted HTTP, document, or browser strategy.",
        ),
    ] = DEFAULT_HTTP_TIMEOUT_SECONDS
    max_retries: Annotated[
        int,
        Field(
            ge=0,
            le=MAX_JOB_MAX_RETRIES,
            description=(
                "Retry count for transient failures inside a selected strategy. Default: 5. "
                f"Per job, so 0-{MAX_JOB_MAX_RETRIES}; the deployment's own default is capped "
                f"lower ({MAX_RETRIES}), which bounds the blast radius of an operator setting "
                "rather than this caller's. Retries do not bypass explicit denial, CAPTCHA, WAF, "
                "login, or paywall classifications."
            ),
        ),
    ] = DEFAULT_MAX_RETRIES
    challenge_wait_seconds: Annotated[
        int,
        Field(
            ge=0,
            le=120,
            description="Passive browser challenge observation budget in seconds for browser flows. Default: "
            "15. The service waits for public content to settle but does not click, solve, outsource, evade, "
            "rotate identities, or inject credentials.",
        ),
    ] = DEFAULT_CHALLENGE_WAIT_SECONDS
    max_discovered_links: Annotated[
        int,
        Field(
            ge=0,
            le=MAX_DISCOVERED_LINKS,
            description="Bounded maximum structured discovered links returned per selected page. Default: "
            "100 (reduced from 250 to cap per-page result memory under parallel crawls). Link counts still "
            "report total discovered before truncation; callers own cross-run frontier queueing, dedupe, "
            "persistence, and recursion (the service collapses URLs that normalize to the same page within a "
            "request).",
        ),
    ] = DEFAULT_MAX_DISCOVERED_LINKS
    respect_robots: Annotated[
        bool,
        Field(
            description="When True (default), /robots.txt rules are honored before fetching and for "
            "recursive-link filtering. Set False only for domains you are authorized to crawl without robots "
            "constraints.",
        ),
    ] = True
    max_depth: Annotated[
        int,
        Field(
            ge=0,
            le=20,
            description="Bounded recursive crawl depth. 0 means a single-page crawl for both /v1/crawl and "
            "/v1/jobs. Positive values admit in-scope, robots-allowed links through that depth; durable jobs "
            "persist each frontier entry's depth.",
        ),
    ] = 0
    max_pages: Annotated[
        int,
        Field(
            ge=0,
            le=2000,
            description="Total-page bound. For /v1/crawl, 0 selects the bounded service default. For "
            "/v1/jobs, 0 or omission disables the page cap; positive values cap canonical page identities "
            "across admission, recovery, and execution. A positive cap that truncates eligible queued or "
            "discovered work sets cap_truncated; already-admitted in-flight work is preserved and recorded "
            "as cap_overcommitted.",
        ),
    ] = 0
    max_rps: Annotated[
        float,
        Field(
            ge=0.0,
            le=100.0,
            description="Maximum requests per second per domain enforced via token bucket. 0 disables rate "
            "limiting (default). When > 0, allows short bursts up to 5 tokens while sustaining the "
            "configured RPS.",
        ),
    ] = 0.0
    max_content_length_bytes: Annotated[
        int | None,
        Field(
            ge=0,
            le=MAX_CONTENT_LENGTH_BYTES,
            description="Maximum content size in bytes per response. Omit (recommended) to let the service "
            "scale the per-page cap against its RAM budget and concurrency; set an explicit value only when "
            "a host needs a hard cap regardless of parallelism. Capped at MAX_CONTENT_LENGTH_BYTES.",
        ),
    ] = None
    total_url_budget_seconds: Annotated[
        float,
        Field(
            ge=0.1,
            le=MAX_TOTAL_URL_BUDGET_SECONDS,
            description="Hard per-URL deadline across the whole strategy chain (seconds). A poisoned/tarpit "
            "URL that exceeds this is failed as network_error instead of holding a concurrency slot. "
            "Default: 180 (the floor of 0.1 only prevents accidental near-zero values; production configs "
            "should keep this generous).",
        ),
    ] = DEFAULT_TOTAL_URL_BUDGET_SECONDS


def _close_browser_profile_vocabulary(profile: dict[str, Any]) -> dict[str, Any]:
    """Refuse inert keys and inert actions in ``browser_profile``, and say what is allowed.

    This model was ``dict[str, Any]`` with exactly one key checked
    (``execute_js``). Everything else a caller could set was prose in a field
    description, which is not enforcement: a misspelled action type passed the
    boundary and was then silently skipped by the renderer's if/elif chain, so
    the caller got a page that loaded with nothing done and nothing said. An
    inert key and a key that worked looked identical.

    The vocabulary is imported from ``app.constants`` rather than restated,
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
    from app.constants import ALLOWED_BROWSER_ACTION_TYPES, BROWSER_PROFILE_KEYS

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
    from app.constants import (
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
    from app.constants import ACTIONS_REQUIRING_SELECTOR, ALLOWED_BROWSER_ACTION_TYPES, BROWSER_ACTION_KEYS

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
    from app.constants import (
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


class Credential(BaseModel):
    """A scoped HTTP credential, scoped to one origin.

    Keyed in ``CrawlPolicy.credentials`` by canonical origin
    (``scheme://host[:port]``), so a credential is addressable only for the
    origin it was written for -- the per-hop redirect scrub for
    ``authorization`` then enforces "credentials never cross host
    boundaries" for free. Values are secrets: never logged, never echoed in
    diagnostics (policy echoes strip them, same discipline as cookies).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bearer", "basic"]
    token: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=512,
            description="Bearer token (kind=bearer).",
        ),
    ] = None
    username: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description="Basic username (kind=basic).",
        ),
    ] = None
    password: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=512,
            description="Basic password (kind=basic).",
        ),
    ] = None

    @model_validator(mode="after")
    def _match_kind(self) -> Credential:
        if self.kind == "bearer" and not self.token:
            raise ValueError("bearer credential requires a token")
        if self.kind == "basic" and not (self.username and self.password):
            raise ValueError("basic credential requires username and password")
        return self


#: Most entries in a crawl's strategy ladder. The ladder runs one outbound
#: network attempt per entry, and this is the only collection on the model that
#: was not capped -- ``urls`` carries 500 and ``headers`` 64 -- so a list of two
#: thousand was a 200 that meant two thousand attempts per URL.
#:
#: A ceiling and not a dedupe, because a repetition is not always waste: a
#: browser render mints a session cookie that a later ``HTTP_CFFI_IMPERSONATE``
#: entry replays, so a non-blocked-but-unusable first attempt can leave the jar
#: in a state that makes its own repetition succeed. Three strategies and a
#: repeat or two is any useful ladder, so 16 bounds the repetition without
#: declaring all of it worthless.
MAX_CRAWL_STRATEGIES: Final[int] = 16

#: The ``request_profile`` key set, as it appears in the OpenAPI document. A sorted
#: list rather than a ``frozenset`` because a set is not JSON serialisable and the
#: document is served; a test reads this back to assert every advertised key has a
#: reader in ``app/``.
_REQUEST_PROFILE_KEYS_JSON: dict[str, Any] = {"keys": sorted(REQUEST_PROFILE_KEYS)}


# ---------------------------------------------------------------------------
# Why each rejected ``policy.headers`` name is rejected.
#
# The map itself is ``CrawlPolicy._BANNED_HEADERS``; the reasoning is here rather
# than in the class body because it is a paragraph about the whole list, and a
# class that has to stay under a size ceiling is the wrong place to keep prose
# that is not about that class. The entries fall into three kinds, and the kinds
# are the reason the list is not shorter:
#
#   * Conditional pairs, which must arrive via ``validators`` so the engine can
#     re-evaluate them per request instead of replaying a stale answer.
#   * Headers an engine chokepoint owns -- ``Host``, ``Cookie``,
#     ``Proxy-Authorization`` -- where a raw header would bypass the thing that
#     makes the hop safe. ``Cookie`` has a second sanctioned door,
#     ``request_profile.cookies``; the reason names both.
#   * Body-framing and hop-by-hop headers, which belong to the HTTP client that
#     frames and negotiates them. A caller cannot describe its own connection,
#     and letting one try corrupts the framing.
#
# The map is the single ban list -- adding an entry there is the whole change
# surface.
# ---------------------------------------------------------------------------


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
                ``app.crawl.credentials.origin_of(url)``, which lowercases the scheme
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
        from app.crawl.credentials import origin_of

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


class _UrlSource(Protocol):
    """Contract shared by every request model that accepts exactly one of ``url``/``urls``."""

    url: str | None
    urls: list[str] | None


#: Bounded by the protocol, not bare: the shared rule has to hand back the
#: concrete model it was handed, or pydantic's after-validator signature check
#: rejects every subclass that inherits it. A TypeVar rather than PEP 695
#: syntax because the floor is 3.11.
_UrlSourceT = TypeVar("_UrlSourceT", bound=_UrlSource)


def _require_exactly_one_url_source[UrlSourceT: _UrlSource](model: UrlSourceT) -> UrlSourceT:
    """Shared url/urls contract for crawl and render requests.

    Both set silently ignored ``url``; neither failed late in
    ``_payload_urls``. Exactly one source is required, and ``urls=[]`` is
    NOT "absent": it dumps through exclude_none and resolves to zero
    targets, so an identity check (not truthiness) gates the contract.
    """
    has_url = model.url is not None
    has_urls = model.urls is not None
    if has_url == has_urls:
        raise ValueError("supply exactly one of 'url' or 'urls'")
    if has_urls and not model.urls:
        raise ValueError("'urls' must contain at least one URL")
    return model


class _SingleUrlSourceRequest(BaseModel):
    """Base for request models that take exactly one of ``url`` / ``urls``.

    Pydantic needs the validator spelled on the model it decorates, so every such
    model carried the same two-line stub. It is a stub, not a decision: the rule
    lives in :func:`_require_exactly_one_url_source`, and a model that wanted to
    opt out had to override the method to *delete* it. The rule is now attached
    once, and joining the family is a base class rather than a copy.

    The two fields are declared here, not left to the subclasses, because the
    validator reads them: a base that enforced a contract it did not itself
    satisfy would type-check only under a cast, and the first subclass to spell
    ``url`` as something other than ``str | None`` would have failed at runtime
    instead of at the type. They are declared *once* here for the same reason --
    a subclass that restated one had to restate the default too, and the two
    descriptions had drifted to differ only in the noun ("to crawl" / "to
    render"), which is not a difference worth a re-declaration per model.
    """

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str | None, Field(description="Single public HTTP(S) URL. Use either url or urls.")] = None
    urls: Annotated[
        list[str] | None,
        Field(
            # Bounded because a whole-site job is the unbounded path: it exists
            # precisely so a caller does not have to enumerate. Without a ceiling
            # a list is unbounded work the service silently agreed to, admitted
            # before any admission check can see it.
            max_length=MAX_URLS_PER_REQUEST,
            description="Batch of public HTTP(S) URLs. Use either url or urls.",
        ),
    ] = None

    @model_validator(mode="after")
    def _require_url_source(self):
        return _require_exactly_one_url_source(self)


class CrawlRequest(_SingleUrlSourceRequest):
    model_config = ConfigDict(extra="forbid")
    # `url`, `urls` and the exactly-one-of validator are inherited, not restated.
    # This model used to redeclare all three, with a comment on the `url`
    # re-declaration admitting it was inherited -- and the re-declared `Field`
    # put its default in the first positional slot, the one shape
    # `test_pydantic_defaults_stay_where_a_checker_can_see_them` exists to ban.
    # A base class that its own subclass copies is not a base class.
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Resolved caller-provided policy/execution plan.",
    )
    validators: dict[str, FetchValidators] = Field(
        default_factory=dict,
        max_length=MAX_VALIDATORS_PER_REQUEST,
        description="Optional map of submitted URL -> cached validators for conditional revalidation. "
        "Keys may be the original or normalized URL. Matching URLs are fetched with "
        "If-None-Match / If-Modified-Since; a 304 produces a not_modified result with no content.",
    )
    async_mode: Annotated[
        bool,
        Field(
            description="When true, accept immediately (202) and run the crawl in the background. Results "
            "are "
            "persisted and available at /v1/crawl/{request_id}. If webhook_url is also provided, the result "
            "is delivered there on completion.",
        ),
    ] = False
    webhook_url: Annotated[
        str | None,
        Field(
            description="Optional callback URL. When provided with async_mode, the crawl is accepted (202) "
            "and the UnifiedCrawlResponse is POSTed to this URL when complete.",
        ),
    ] = None
    output_format: Annotated[
        OutputFormat | None,
        Field(
            description="Multiple output formats for the crawl results. One of: json (default), jsonl, "
            "ndjson, markdown, csv. Non-json formats are returned as a plain Response with the matching "
            "media type instead of a UnifiedCrawlResponse model.",
        ),
    ] = None


class MapRequest(BaseModel):
    """Site-map request: discover the URL frontier for a root URL.

    Discovery (robots sitemaps + feeds + in-page links) is forced on; the caller
    controls the fetch ``policy`` (strategies, limits, scope_regex) and the rest
    is delegated to the shared crawl engine.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="Root public HTTP(S) URL to map.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Optional crawl policy for the mapping crawl (strategies, limits, scope_regex). "
        "Discovery "
        "is always enabled.",
    )


class SearchRequest(BaseModel):
    """Request body for ``POST /v1/search``.

    A web-search query that returns SERP-style results (title, url,
    description) from the configured provider. The service does not crawl
    result URLs; callers pipe them into ``POST /v1/crawl`` themselves.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500, description="Web search query.")
    limit: Annotated[
        int | None,
        Field(
            ge=1,
            le=100,
            description="Maximum results to return. Defaults to the server setting "
            "CRAWLUTRON_SEARCH_LIMIT_DEFAULT and is clamped to "
            "CRAWLUTRON_SEARCH_LIMIT_MAX.",
        ),
    ] = None


class RenderOptionsRequest(BaseModel):
    """Browser render options for ``/v1/render`` (all optional; server applies defaults)."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: Annotated[
        int | None,
        Field(
            description="Per-render navigation timeout in seconds.",
        ),
    ] = None
    max_retries: Annotated[
        int | None,
        Field(
            description="Maximum retries for a transient render failure.",
        ),
    ] = None
    respect_robots: Annotated[
        bool | None,
        Field(
            description="Whether to honor robots.txt for the render target.",
        ),
    ] = None
    include_html: Annotated[bool | None, Field(description="Include the rendered HTML in the result.")] = None
    include_text: Annotated[
        bool | None,
        Field(
            description="Include extracted visible text in the result.",
        ),
    ] = None


class RenderRequest(_SingleUrlSourceRequest):
    """Request body for ``POST /v1/render`` (backward-compatible browser render)."""

    model_config = ConfigDict(extra="forbid")

    # `url`, `urls` and the exactly-one-of validator are inherited, not restated --
    # see the note on CrawlRequest.
    options: Annotated[
        RenderOptionsRequest | None,
        Field(
            description="Browser render options; omitted fields use server defaults.",
        ),
    ] = None


class JobCreateRequest(BaseModel):
    """Create a new resumable whole-site crawl job."""

    model_config = ConfigDict(extra="forbid")

    root_url: str = Field(..., description="Root public HTTP(S) URL; stored canonically at crawl depth 0.")
    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Crawl policy (strategies, limits, scope_regex, extraction).",
    )


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="One of: pause, resume, cancel.")
