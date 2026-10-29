from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from crawlutron.models.caller_key import CallerKey


class KeySummary(BaseModel):
    """One key's public inventory row: everything but the hash and secret."""

    model_config = ConfigDict(extra="ignore")

    key_id: str
    label: str = ""
    max_pages: int = 0
    max_concurrency: int = 0
    created_at: float = 0.0

    @classmethod
    def from_record(cls, record: CallerKey) -> KeySummary:
        """The public row for a stored record, built field by field."""
        return cls(
            key_id=record.key_id,
            label=record.label,
            max_pages=record.max_pages,
            max_concurrency=record.max_concurrency,
            created_at=record.created_at,
        )
