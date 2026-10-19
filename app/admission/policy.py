"""The one read edge between a policy as it arrives and the model that reads it:
read_policy is total (defaults, the safe direction), validate_wire_policy rejects,
and the operator tier is shared so API- and job-path defaults agree.
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
ERROR_CODE_BY_FIELD: dict[str, str] = {
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
    """Resolve the policy's discovery channels once, absence included.
    Every channel defaults to off (``true`` alone turns one on), so an absent
    block, an explicit ``{"discovery": {}}``, and a pre-field stored row all mean fetch nothing.
    """
    discovery = policy.discovery
    if discovery is None:
        return DiscoveryFlags(False, False, False)
    return DiscoveryFlags(discovery.robots, discovery.sitemaps, discovery.feeds)


@dataclass(frozen=True, slots=True)
class PolicyTier:
    """A deployment's answer for limit fields a caller may omit, resolved once so an
    omitted field is the same number on the API and job paths (threading settings
    made it optional per site, so /v1/crawl honored the default but /v1/jobs did not).
    """

    timeout_seconds: int
    max_retries: int

    @classmethod
    def model_defaults(cls) -> PolicyTier:
        """The tier a policy gets with no deployment opinion: the model's own.
        A named value rather than ``None``, so "nobody stated a tier" and
        "the deployment said the model's defaults" cannot be confused; a default is a tier value too.
        """
        limits = CrawlPolicyLimits()
        return cls(timeout_seconds=limits.timeout_seconds, max_retries=limits.max_retries)

    @classmethod
    def from_settings(cls, settings: CrawlerSettings) -> PolicyTier:
        """The deployment's numbers, as the settings chokepoint resolved them -- no
        re-clamping: ``from_mapping`` is the single bound, by the deployment's own
        authority, not the model's per-request bound; that invariant is now a constants check.
        """
        return cls(
            timeout_seconds=int(settings.default_timeout_seconds),
            max_retries=int(settings.default_max_retries),
        )

    def applied(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "max_retries": self.max_retries}


def _with_omitted_defaults(raw: dict[str, Any], tier: PolicyTier | None) -> dict[str, Any]:
    """Fill the caller's omitted fields, leaving every stated value alone.
    Non-mapping fields pass through so the model reports its own shape error
    ("limits" not a mapping used to raise into a 500); setdefault on a copy, never the caller's dict.
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
    Deepest mapped location wins, so a bad ``limits.max_depth`` beats a top-level
    error. ``strategies`` ranks its two codes rather than returning early, keeping "deepest wins" true.
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
        elif field in ERROR_CODE_BY_FIELD:
            code = ERROR_CODE_BY_FIELD[field]
        else:
            continue
        ranked.append((len(loc), code))
    return max(ranked)[1] if ranked else "invalid_policy"


def _without(raw: dict[str, Any], path: list[Any]) -> dict[str, Any] | None:
    """``raw`` with ``path`` removed, or ``None`` when it is not there.
    Pure and copy-on-write along the path, so a repair cannot mutate the payload
    the API echoes back; only path mappings copy, so a large block pays for the path, not the tree.
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
    """The policy with every location the model rejected removed, deepest first: a
    bad ``limits.max_retries`` drops the retry bound, not the whole limits block.
    A list-index location drops the whole list (elements renumber); ``None`` signals nothing was removable.
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
    """``raw`` as the one of three things a policy edge can act on: a model (handed
    straight back -- re-validating one used to reject it as "invalid_policy"), a
    mapping to validate, or ``None`` (where the total read and refusing wire edges differ).
    """
    if isinstance(raw, CrawlPolicy):
        return raw
    return raw if isinstance(raw, dict) else None


def read_policy(raw: Any, tier: PolicyTier | None = None) -> CrawlPolicy:
    """Total read edge: any input resolves to a policy, repairing what failed.
    Repair is field-scoped (whole-default discards what the caller got right; the read edge never raises).
    Drop the rejected locations and re-validate, ending at full defaults only when nothing remains.
    """
    resolved = _policy_input(raw)
    if not isinstance(resolved, dict):
        return resolved if resolved is not None else CrawlPolicy()
    candidate = _with_omitted_defaults(resolved, tier)
    limits = candidate.get("limits")
    if limits is None or (isinstance(limits, Mapping) and "max_pages" not in limits):
        # #313: an omitted max_pages defaults on the read edge too, matching the
        # wire edge; an explicit 0 alone keeps the model's unbounded meaning.
        merged = dict(limits) if isinstance(limits, Mapping) else {}
        merged["max_pages"] = DEFAULT_MAX_PAGES
        candidate = {**candidate, "limits": merged}
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
    A stored row keeps the model's own dump because ``CrawlJobState`` is JSON,
    rather than a second spelling; nothing downstream of read_policy sees this shape.
    """
    return read_policy(policy).model_dump()


def validate_wire_policy(raw: Any, tier: PolicyTier | None = None) -> CrawlPolicy:
    """Validate a caller's policy, rejecting with the code the API contracts for.
    Every rule is the model's own: per-field codes derive from where the model
    reports the error; an already-built model returns as-is, since re-validating one used to reject it.
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
