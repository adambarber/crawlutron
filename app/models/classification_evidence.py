from __future__ import annotations

from typing import Annotated, TypedDict

from pydantic import BaseModel, Field


class ClassificationEvidenceRecord(TypedDict):
    """The key set a classification-evidence record carries at runtime; the
    Pydantic model below is the wire form of the same record."""

    classification: str
    signature: str
    source: str
    context: str


class ClassificationEvidence(BaseModel):
    classification: str = Field(description="Classification selected by this matched evidence.")
    signature: str = Field(description="Signature that matched the selected source bucket.")
    source: str = Field(
        description="Evidence source bucket, such as extracted_text, html_without_scripts_styles, or "
        "http_status."
    )
    context: Annotated[
        str,
        Field(
            description="Short normalized context around the matched signature.",
        ),
    ] = ""
