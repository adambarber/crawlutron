from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobDeletedResponse(BaseModel):
    """The delete outcome as a typed body; the REST DELETE door points here too."""

    model_config = ConfigDict(extra="ignore")

    deleted: bool = Field(description="Whether the job row was removed.")
    job_id: str = Field(description="Identifier of the deleted crawl job.")
