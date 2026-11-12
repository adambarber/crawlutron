"""The pre-flight admission request: one raw URL to judge, no policy carrier.

Admission input models carry the raw ``url`` (see ``SingleUrlSourceRequest``);
conversion to an admitted value happens at the endpoint edge.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class AdmitRequest(BaseModel):
    """One URL asked of the admission contract before any fetch is scheduled."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="The raw URL to admit or refuse.")
    check_robots: Annotated[
        bool,
        Field(description="Opt-in robots.txt check; the only I/O path this endpoint has."),
    ] = False


__all__ = ["AdmitRequest"]
