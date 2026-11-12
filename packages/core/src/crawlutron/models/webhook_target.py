"""WebhookTarget: the admitted, secret-carrying webhook destination (#599).

The raw URL is admitted through the ONE SSRF chokepoint; every projection is
redacted, and the raw URL escapes only via ``delivery_url()`` at the POST site.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.url_redaction import is_secret_name, redact_url


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

        Userinfo is refused by NormalizedUrl's ``userinfo_refused`` law; a secret-
        named query parameter is refused too (it would persist plaintext at rest).
        """
        parsed = urlsplit(raw)
        secret = next(
            (name for name, _ in parse_qsl(parsed.query, keep_blank_values=True) if is_secret_name(name)),
            None,
        )
        if secret:
            raise ValueError(
                f"webhook URL query parameter {secret!r} carries credential-shaped vocabulary"
                " and is refused; webhook auth is policy credentials, never URL params"
            )
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
