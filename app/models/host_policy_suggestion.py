"""A candidate host policy rule proposed from classification evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.models.classification_evidence import ClassificationEvidence
from app.vocabulary.taxonomy import CrawlClassification

CandidateRule = Literal["denied_hosts"]
"""The host policy rules evidence can support.

One member, and that is the whole point of naming it: the only rule repeated
block evidence supports is a deny entry, and a caller who wants "allow this
host" is answering a different question than the one this surface answers.
"""


class HostPolicySuggestion(BaseModel):
    """One host's candidate rule, with the evidence that proposed it.

    A proposal, never an application. The service has no path that writes host
    policy from a crawl result, so the only way this becomes real is a person
    reading ``rationale`` and the evidence beneath it and deciding.
    """

    host: str = Field(description="Registrable site this candidate rule is about.")
    rule: Annotated[
        CandidateRule,
        Field(description="Candidate host policy rule. Never applied by this service."),
    ]
    blocked_attempts: int = Field(description="Attempts on this host that came back blocked.")
    observed_attempts: int = Field(description="Attempts on this host across the retained records.")
    classifications: list[CrawlClassification] = Field(
        description="Distinct block classifications observed on this host."
    )
    signatures: list[str] = Field(description="Distinct evidence signatures observed, in first-seen order.")
    evidence: list[ClassificationEvidence] = Field(
        description="A bounded sample of the evidence, for review."
    )
    rationale: str = Field(description="One sentence stating the counts and the candidate rule.")
