"""The durable state of one registered watch, as a row and on the wire."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, field_serializer
from safe_url.url_redaction import redact_url

from crawlutron.models.content_validators import ContentValidators
from crawlutron.models.watch_status import WatchStatus
from crawlutron.vocabulary.taxonomy import CrawlClassification

if TYPE_CHECKING:
    from safe_url.host_policy import HostPolicy

    from crawlutron.models.webhook_target import WebhookTarget

IntervalSeconds = Annotated[
    int,
    Field(gt=0, description="Seconds between revalidation checks of this watch."),
]


class WatchState(BaseModel):
    """One row of the watches table, in the shape the store and API share.

    ``url`` stores the durable text identity, re-admitted against live host
    policy. The notification target is a CREDENTIAL: leaves only redacted.
    """

    watch_id: str
    # The caller that owns this row: "" (anonymous bucket) or a keyed caller's
    # id. Registry CRUD is owner-scoped; the sweep reads it to inherit cookies.
    owner: Annotated[
        str,
        Field(description="Owner id (empty means the anonymous bucket)."),
    ] = ""
    url: str
    # The change-notification target's durable text. Serialized, repr'd and
    # logged REDACTED; the raw form escapes only through delivery_url().
    webhook_url: Annotated[
        str | None,
        Field(description="Change-notification target; absent means a poll-only watch."),
    ] = None

    @field_serializer("webhook_url", when_used="always")
    def _serialize_webhook_url(self, value: str | None) -> str | None:
        """The serialized form is the log-safe form: the raw URL is delivery_url()."""
        return None if value is None else redact_url(value)

    interval_seconds: IntervalSeconds
    status: WatchStatus = WatchStatus.ACTIVE
    validators: ContentValidators = Field(default_factory=ContentValidators)
    next_check_at: float = Field(description="Epoch time of the next scheduled check.")
    last_checked_at: float | None = None
    last_outcome: CrawlClassification | None = None
    last_changed_at: float | None = None
    consecutive_failures: Annotated[int, Field(ge=0)] = 0
    created_at: float
    updated_at: float

    def target(self, host_policy: HostPolicy) -> WebhookTarget | None:
        """The admitted delivery target for this watch's webhook, or None.

        Admission runs through the ONE chokepoint (NormalizedUrl) against the
        caller's live host policy -- the durable text is never trusted alone.
        """
        if self.webhook_url is None:
            return None
        from crawlutron.models.webhook_target import WebhookTarget

        return WebhookTarget.admit(self.webhook_url, host_policy=host_policy)

    def delivery_url(self) -> str | None:
        """The raw webhook URL, ONLY for the code that actually delivers."""
        return self.webhook_url

    def redacted_url(self) -> str:
        """The log/metric form of the watch's own URL: secrets masked."""
        return redact_url(self.url)

    def redacted_webhook_url(self) -> str | None:
        """The log/serial form of the notification target: secrets masked."""
        return None if self.webhook_url is None else redact_url(self.webhook_url)

    def __repr__(self) -> str:
        shown = self.redacted_webhook_url()
        fields = ", ".join(
            f"{name}={shown if name == 'webhook_url' else value!r}" for name, value in self.__dict__.items()
        )
        return f"WatchState({fields})"
