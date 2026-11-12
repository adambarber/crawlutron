"""The exactly-one-of-url/urls contract, and the base that carries it.

Pydantic needs a validator on the model it decorates, so joining this family is
a base class rather than a copy of the same two-line stub.
"""

from __future__ import annotations

from typing import Annotated, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Policy, not schema: a whole-site job is the unbounded path, so without a ceiling a
#: list is unbounded work agreed to before any admission check can see it.
MAX_URLS_PER_REQUEST = 500


class _UrlSource(Protocol):
    """Contract shared by every request model taking exactly one of url/urls."""

    url: str | None
    urls: list[str] | None


#: Bound, not bare: the rule must return the concrete model it was handed, or
#: pydantic's after-validator signature check rejects every subclass.
_UrlSourceT = TypeVar("_UrlSourceT", bound=_UrlSource)


def _require_exactly_one_url_source[UrlSourceT: _UrlSource](model: UrlSourceT) -> UrlSourceT:
    """Exactly one source; `urls=[]` is not absent, so identity not truthiness gates it."""
    has_url = model.url is not None
    has_urls = model.urls is not None
    if has_url == has_urls:
        raise ValueError("supply exactly one of 'url' or 'urls'")
    if has_urls and not model.urls:
        raise ValueError("'urls' must contain at least one URL")
    return model


class SingleUrlSourceRequest(BaseModel):
    """Base for models taking exactly one of url/urls; the rule is not a stub.

    The fields live here, not in subclasses: the validator reads them, and a subclass
    that restated one had to restate the default too.
    """

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str | None, Field(description="Single public HTTP(S) URL. Use either url or urls.")] = None
    urls: Annotated[
        list[str] | None,
        Field(
            max_length=MAX_URLS_PER_REQUEST,
            description="Batch of public HTTP(S) URLs. Use either url or urls.",
        ),
    ] = None

    @model_validator(mode="after")
    def _require_url_source(self):
        return _require_exactly_one_url_source(self)
