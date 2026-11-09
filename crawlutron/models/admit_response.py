"""The pre-flight admission response: verdict, per-check breakdown, canonical form.

Every member of the breakdown reuses the existing admission refusal vocabulary
(the constructor's own reasons plus robots); nothing policy-internal is echoed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from crawlutron.models.normalized_url import NormalizedUrl

#: The refusal vocabulary the endpoint reports, one spelling of the constructor's
#: reasons plus the robots arm.
AdmitReason = Literal[
    "invalid_url",
    "unsupported_scheme",
    "userinfo_refused",
    "ipvfuture_refused",
    "host_denied",
    "host_not_allowed",
    "private_network_target",
    "robots_disallowed",
]

#: The per-check table: key order is the order the checks run; None is a pass.
AdmitChecks = dict[str, AdmitReason | None]


class AdmitResponse(BaseModel):
    """The admission verdict with the per-check reason table."""

    model_config = ConfigDict(extra="allow")

    admitted: bool = Field(..., description="Whether the URL would be admitted for a fetch.")
    classification: Annotated[
        AdmitReason | None, Field(description="The refusal classification; None when admitted.")
    ] = None
    canonical_url: Annotated[
        NormalizedUrl | None,
        Field(description="The canonical form; present only when the URL itself is valid."),
    ] = None
    checks: AdmitChecks = Field(..., description="Per-check reasons; None where the check passed.")


__all__ = ["AdmitChecks", "AdmitReason", "AdmitResponse"]
