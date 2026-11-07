"""WebhookTarget: the admitted, secret-carrying webhook destination (#599).

The raw URL enters the constructor and is admitted through the ONE SSRF
chokepoint -- ``NormalizedUrl(raw, policy=...)``, which refuses userinfo under
the shared ``userinfo_refused`` law. Every projection (str, repr, serialization,
log/metric/error paths) is redacted; the raw admitted URL escapes only via
``delivery_url()``, at the POST site alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.url_redaction import redact_url


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """One webhook destination: admitted at construction, redacted by default.

    Build one at the door (where a host policy exists); the constructor's
    ``NormalizedUrl`` admission IS the SSRF validation -- no second dialect.
    """

    target: NormalizedUrl

    @classmethod
    def admit(cls, raw: str, *, host_policy: HostPolicy) -> WebhookTarget:
        """Admit a raw webhook URL through the shared SSRF chokepoint.

        Userinfo (``user:pass@host``) is refused by NormalizedUrl's own
        ``userinfo_refused`` law; webhook auth is policy.credentials, never
        credentials in the URL.
        """
        return cls(target=NormalizedUrl(raw, policy=host_policy))

    def delivery_url(self) -> str:
        """The raw admitted URL, ONLY for the code that actually delivers."""
        return self.target.raw

    def redacted(self) -> str:
        """The log/metric/wire form: userinfo and secret query values masked.

        Userinfo never appears (admission refused it); this covers the residual
        query-secret class on the canonical, public form.
        """
        return redact_url(self.target.raw)

    def __str__(self) -> str:
        return self.redacted()

    def __repr__(self) -> str:
        return f"WebhookTarget({self.redacted()!r})"


__all__ = ["WebhookTarget"]
