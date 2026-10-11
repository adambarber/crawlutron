"""The one read edge between a policy as it arrives and the model everything reads.

Two producers fed this shape before, and they disagreed. ``payload_policy``
validated a wire payload field by field, re-spelling every bound the model
already declares; ``job_store.normalized_policy`` merely copied a stored dict
and filled in nothing, so a stored job policy that predated a field -- or was
corrupt text -- resolved every limit to ``0``. That included
``respect_robots``, which read as *do not obey robots.txt*. The API path always
filled the defaults, which is precisely why a suite that exercised the API path
for nearly everything stayed green while the job path quietly dropped the
protection.

Both edges come through here now, so "what does a policy that did not say X
mean" has one answer:

* :func:`read_policy` is total. Absent, wrong-shaped, or unreadable resolves to
  the model's declared defaults -- the safe direction, because the dangerous
  direction for a limit is the one that widens it.
* :func:`validate_wire_policy` is the API edge. It rejects instead, and it
  *derives* the ``invalid_policy_*`` code from where the model reports the
  error, so a code cannot drift from the declaration it names.

The operator tier (:func:`operator_limits`) is the third layer and is shared
deliberately: a deployment's default for a field a caller may omit has to be
the same number on the API path and the job path, or the two paths describe
different crawls. That was the original defect in a different costume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError

from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_policy_limits import CrawlPolicyLimits

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.config.settings import CrawlerSettings

__all__ = [
    "DEFAULT_MAX_PAGES",
    "DiscoveryFlags",
    "PolicyTier",
    "discovery_flags",
    "read_policy",
    "stored_policy",
    "validate_wire_policy",
]

# 0 is the model's own legal value (unbounded, whole-site runner), so the
# absent/zero translation happens before validation and only for that case.
DEFAULT_MAX_PAGES = 100

# Keyed on the model's own field names (test_every_model_field_names_a_code keeps
# it so): a field with no code reports a bare invalid_policy, not which part.
_ERROR_CODE_BY_FIELD: dict[str, str] = {
    "schema_version": "invalid_policy",
    "strategies": "invalid_policy_strategies",
    "extraction": "invalid_policy_extraction",
    "limits": "invalid_policy_limits",
    "headers": "invalid_policy_headers",
    "credentials": "invalid_policy_credentials",
    "request_profile": "invalid_policy_profile",
    "browser_profile": "invalid_policy_profile",
    "discovery": "invalid_policy_profile",
    "scope_regex": "invalid_policy",
    "css_selector": "invalid_policy",
    "xpath": "invalid_policy",
}


class DiscoveryFlags(NamedTuple):
    """Which discovery channels a policy turns on."""

    robots: bool
    sitemaps: bool
    feeds: bool


def discovery_flags(policy: CrawlPolicy) -> DiscoveryFlags:
    """Resolve a policy's discovery channels once, absence included.

    Every channel defaults to off -- ``true`` is the only thing that turns one
    on -- so an absent block and an explicit ``{"discovery": {}}`` are the same
    policy and have to be the same answer: fetch nothing. A stored row written
    before the field existed is absent too, so it lands here as well.

    Deciding that here is the whole point of the function. Reading the optional
    field at each call site is how the two answers drifted apart to begin with:
    a defaulted lookup over the raw mapping could not tell an absent channel
    from an explicit ``false``, and the block being optional meant a policy that
    never mentioned discovery arrived as an empty mapping rather than as a model
    whose declared defaults had something to say about it.
    """
    discovery = policy.discovery
    if discovery is None:
        return DiscoveryFlags(False, False, False)
    return DiscoveryFlags(discovery.robots, discovery.sitemaps, discovery.feeds)


@dataclass(frozen=True, slots=True)
class PolicyTier:
    """A deployment's answer for limit fields a caller may omit.

    Exists as a value rather than a ``CrawlerSettings`` argument because the
    invariant it carries -- "a field the caller omitted means the same number on
    the API path and the job path" -- is only enforceable if the deployment
    resolves it once and every read edge is handed the result. Threading the
    settings themselves made it optional at each site, and optional at each site
    is the same as absent at one of them: every job-path read passed ``None``,
    so ``/v1/crawl`` honoured ``CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS`` and
    ``/v1/jobs`` did not, from the same deployment, on the same policy.

    Bounded at the settings chokepoint by the deployment's own authority, which
    is deliberately NOT the model's per-request bound: a deployment-wide default
    has the blast radius of every caller, so it is capped lower, while a single
    job's caller may ask for more.
    """

    timeout_seconds: int
    max_retries: int

    @classmethod
    def model_defaults(cls) -> PolicyTier:
        """The tier a policy gets with no deployment opinion: the model's own.

        A named value rather than ``None`` so "nobody stated a tier" and
        "the deployment said the model's defaults" cannot be confused, and so
        a default is a decision on the same type as every other tier.
        """
        limits = CrawlPolicyLimits()
        return cls(timeout_seconds=limits.timeout_seconds, max_retries=limits.max_retries)

    @classmethod
    def from_settings(cls, settings: CrawlerSettings) -> PolicyTier:
        """The deployment's numbers, as the settings chokepoint resolved them.

        No re-clamping here. ``CrawlerSettings.from_mapping`` is the single place
        a deployment's numbers are bounded, and it bounds them by the
        *deployment's* authority (an operator retry ceiling lower than the
        per-job one), which is not the bound the policy model declares. Reading
        the model's bound here was a second enforcement site with different
        semantics: it silently repaired a bad deployment value that the
        chokepoint had already warned about, so the warning and the resulting
        number could disagree. The invariant this function used to protect
        instead -- an operator cannot install a value a caller would be rejected
        for -- is a standing check on the constants
        (``test_the_deployment_retry_ceiling_is_requestable``), because it is a
        property of the numbers, not of a runtime value.
        """
        return cls(
            timeout_seconds=int(settings.default_timeout_seconds),
            max_retries=int(settings.default_max_retries),
        )

    def applied(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "max_retries": self.max_retries}


def _with_omitted_defaults(raw: dict[str, Any], tier: PolicyTier | None) -> dict[str, Any]:
    """Fill the fields a caller omitted, leaving every stated value alone.

    A field that is not a mapping is passed through untouched so the model
    reports its own shape error -- reaching into it here is how a bad
    ``limits`` raised ``TypeError`` out of the validator and became a 500 where
    the caller owed a 400.

    ``setdefault`` on a copy, not on the caller's dict: the input is a payload
    the API layer is about to echo, and mutating it is how "we did not touch
    the caller's policy" stopped being true.
    """
    # Absent needs the operator tier applied; present-but-wrong-shape is a caller
    # error the model reports. Collapsing the two skipped both.
    if tier is None or (raw.get("limits") is not None and not isinstance(raw["limits"], Mapping)):
        return raw
    limits = dict(raw.get("limits") or {})
    for key, default in tier.applied().items():
        limits.setdefault(key, default)
    return {**raw, "limits": limits}


def _code_for(exc: ValidationError) -> str:
    """The API error code for a validation failure, derived from its location.

    Deepest mapped location wins, so a bad ``limits.max_depth`` reports the
    limits code even when a top-level field also failed -- the caller needs to
    know which part of their policy to fix, and the limits are the specific
    answer.

    ``strategies`` is the one field with two codes, and the choice is made by
    rank rather than by returning early. The model reports a name outside the
    enum and a member that is not a string at all identically -- for a Literal
    field both are "not one of the allowed values" -- so the offending value is
    what separates them. Naming a strategy we do not support is one caller
    mistake (``invalid_policy_strategy``); sending a malformed list is another
    (``invalid_policy_strategies``), and the API contract has always said which
    was which. Ranking it alongside the other candidates rather than returning
    from inside the loop is what keeps the "deepest wins" rule true when a
    strategy error and a deeper limits error arrive together; an early return
    made the answer depend on where ``strategies`` happened to be declared in
    the model.
    """
    ranked: list[tuple[int, str]] = []
    for detail in exc.errors():
        loc = detail["loc"]
        field = str(loc[0]) if loc else ""
        if field == "strategies" and detail["type"] == "enum":
            code = (
                "invalid_policy_strategy"
                if isinstance(detail.get("input"), str)
                else "invalid_policy_strategies"
            )
        elif field in _ERROR_CODE_BY_FIELD:
            code = _ERROR_CODE_BY_FIELD[field]
        else:
            continue
        ranked.append((len(loc), code))
    return max(ranked)[1] if ranked else "invalid_policy"


def _without(raw: dict[str, Any], path: list[Any]) -> dict[str, Any] | None:
    """``raw`` with ``path`` removed, or ``None`` when it is not there.

    Pure and copy-on-write along the path: a repair that mutated its input
    would be invisible to the caller but visible to the API layer, which echoes
    this payload back -- the response would under-report what was sent. Only the
    mappings on the path are copied, so a policy carrying a large credential
    block pays for the path and not the tree.
    """
    chain: list[dict[str, Any]] = [raw]
    node: Any = raw
    for part in path[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
        chain.append(node)
    leaf = path[-1]
    holder = chain[-1]
    if not isinstance(leaf, str) or leaf not in holder:
        return None
    rebuilt: dict[str, Any] = {key: value for key, value in holder.items() if key != leaf}
    for mapping, part in reversed(list(zip(chain[:-1], path[:-1], strict=True))):
        rebuilt = {**mapping, part: rebuilt}
    return rebuilt


def _drop_rejected(raw: dict[str, Any], exc: ValidationError) -> dict[str, Any] | None:
    """The policy with every location the model rejected removed.

    Deepest first, so a bad ``limits.max_retries`` drops the retry bound rather
    than the caller's whole limits block -- the neighbouring ``timeout_seconds``
    is a different decision, stated by the same caller in the same breath. A
    location that names a list index drops the whole list: an element removed
    from the middle renumbers the rest, and ``strategies`` is an ordered
    execution chain where that would promote one strategy into another's slot.

    Returns ``None`` when nothing was removable, which is the caller's signal
    that the repair is finished and the remaining answer is the declared
    defaults.
    """
    candidate = raw
    dropped = False
    for detail in sorted(exc.errors(), key=lambda item: -len(item["loc"])):
        loc: list[Any] = list(detail["loc"])
        if not loc:
            continue
        if any(isinstance(part, int) for part in loc[1:]):
            loc = loc[:-1]
        if not loc:
            continue
        trimmed = _without(candidate, loc)
        if trimmed is not None:
            candidate, dropped = trimmed, True
    return candidate if dropped else None


#: What the read edge accepts: a policy already through :func:`read_policy`, or
#: the raw mapping a request payload carries -- two forms, never ``Any``.
PolicyLike = CrawlPolicy | dict[str, Any]


def _policy_input(raw: Any) -> CrawlPolicy | dict[str, Any] | None:
    """``raw`` as the one of three things a policy edge can act on.

    A model is already validated and must be handed straight back -- validating
    it again is not merely wasted work, it is how the strict edge used to reject
    a valid policy as ``invalid_policy`` for not being a mapping. A mapping is
    something to validate. ``None`` is neither, and it is the *only* case where
    the two policy edges genuinely differ: the read edge answers with defaults
    because it is total, the wire edge answers with a refusal because it is
    not.

    Stated once because it was stated twice, and the two copies had drifted
    apart on the question that matters -- whether a re-read of a broken model
    is even possible. It is not: a constructed model is valid by construction,
    so returning one untouched is not a shortcut, it is the only correct answer.
    """
    if isinstance(raw, CrawlPolicy):
        return raw
    return raw if isinstance(raw, dict) else None


def read_policy(raw: Any, tier: PolicyTier | None = None) -> CrawlPolicy:
    """Total read edge: any input resolves to a policy, repairing what failed.

    A row the write edge produced is always valid, so a stored policy that
    fails here is corrupt text or a pre-schema-version row.
    The repair is field-scoped, and the reason it is not "fall back to the
    whole declared default" is that the whole-default answer discards the
    fields the caller got right. One retry bound over the ceiling used to
    replace a narrower timeout, an explicit strategy chain, the browser
    profile, and ``respect_robots`` -- with no error and no log, because the
    read edge by contract cannot raise. That is the shadow-dict failure mode
    again wearing a different hat: an operator's stated choice that vanishes.
    The dangerous direction for a limit is the one that widens it, and total
    defaults widened every field at once.

    The rule is written once rather than per field, because pydantic already
    names what it rejected: drop those locations and re-validate, and whatever
    the model then accepts is the answer. It terminates because each pass
    removes at least one key, and it ends at full defaults only when there is
    nothing left to drop.
    """
    resolved = _policy_input(raw)
    if not isinstance(resolved, dict):
        return resolved if resolved is not None else CrawlPolicy()
    candidate = _with_omitted_defaults(resolved, tier)
    for _ in range(len(CrawlPolicy.model_fields)):
        try:
            return CrawlPolicy.model_validate(candidate)
        except ValidationError as exc:
            trimmed = _drop_rejected(candidate, exc)
            if trimmed is None:
                break
            candidate = trimmed
    return CrawlPolicy()


def stored_policy(policy: Any) -> dict[str, Any]:
    """The write edge, mirroring :func:`read_policy` in the other direction.

    A stored row keeps a dict because ``CrawlJobState`` is JSON, so the
    persistence shape is the model's own dump rather than a second spelling of
    it. Nothing downstream of :func:`read_policy` sees this shape.
    """
    return read_policy(policy).model_dump()


def validate_wire_policy(raw: Any, tier: PolicyTier | None = None) -> CrawlPolicy:
    """Validate a caller's policy, rejecting with the code the API contracts for.

    Every rule is the model's own. The per-field codes come from where the
    model reports the error, so adding a bound to a field cannot leave this
    function enforcing something different from the model -- the failure mode
    of the hand-spelled version, which had drifted far enough that it read
    ``limits`` three different ways in one file.

    An already-built model is returned as it stands. The whole-site runner
    holds one (it read the policy out of a job row) and hands it to the engine
    through the raw payload shape, so re-validating a validated policy is not
    just wasted work -- it was rejecting it, as ``invalid_policy``, for not
    being a dict.
    """
    resolved = _policy_input(raw)
    if not isinstance(resolved, dict):
        if resolved is not None:
            return resolved
        raise ValueError("invalid_policy") from None
    raw_limits = resolved.get("limits")
    if raw_limits is not None and not isinstance(raw_limits, Mapping):
        # A caller sent a non-mapping limits: hand the value to the model untouched
        # so it reports the shape, which lands on invalid_policy_limits.
        try:
            return CrawlPolicy.model_validate(_with_omitted_defaults(resolved, tier))
        except ValidationError as exc:
            raise ValueError(_code_for(exc)) from None
    limits = dict(raw_limits or {})
    # A missing/zero/negative max_pages is bounded, never "unlimited": the model
    # bounds a positive request itself, and 0 is its own legal meaning.
    requested_pages = limits.get("max_pages", 0)
    try:
        if int(requested_pages or 0) <= 0:
            limits["max_pages"] = DEFAULT_MAX_PAGES
    except (TypeError, ValueError):
        raise ValueError("invalid_policy_limits") from None
    # A body cap of 0 and an absent cap mean the same thing to the consumer
    # (`if explicit:`), and 0 is what the wire echo has always carried.
    limits.setdefault("max_content_length_bytes", 0)
    try:
        return CrawlPolicy.model_validate(_with_omitted_defaults({**resolved, "limits": limits}, tier))
    except ValidationError as exc:
        raise ValueError(_code_for(exc)) from None
