"""The one cookie-retry chain scenario the cffi-retry tests share.

Both the cookie-bootstrap contract and the retry's attempt-counting drive the
same flow -- cookies planted, http denies, cffi succeeds only WITH the cookie,
browser renders -- so it is written once (jscpd's zero clone-pairs) here.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from tests._browser_doubles import _fake_browser
from tests._chain_policies import chain_policy
from tests._fetch_doubles import FetcherDouble
from tests._fetcher_factories import run_chain

if TYPE_CHECKING:
    pass

#: A browser worker's success shape, which is what the strategy chain needs back once the cffi hop has the
#: cookies.
RENDERED = _fake_browser(html="<html>rendered</html>", text="rendered")

DENIED = (403, b"denied", "text/plain")


def cffi_retry_chain(
    *,
    on_cffi: Callable[[str, dict[str, str]], tuple[int, bytes, str]] | None = None,
):
    """The full chain over the cookie-retry ladder: http denies, cffi answers
    ``on_cffi`` (the cookie oracle), the browser renders."""
    from crawlutron.vocabulary.strategies import (
        BROWSER_HEADLESS,
        HTTP_CFFI_IMPERSONATE,
        HTTP_DECLARED_CRAWLER,
    )

    return run_chain(
        fetcher=FetcherDouble(lambda url, headers, *_: DENIED),
        cffi_fetcher=FetcherDouble(lambda url, headers, *_: (on_cffi or (lambda u, h: DENIED))(url, headers)),
        browser_worker=lambda job: RENDERED(job),
        policy=chain_policy(
            HTTP_DECLARED_CRAWLER,
            HTTP_CFFI_IMPERSONATE,
            BROWSER_HEADLESS,
            discovery={"robots": True},
        ),
    )


def cookie_oracle(marker: str, *, record: list[dict[str, str]] | None = None):
    """The cffi hop answers 200 only when the replayed cookie carries ``marker``
    (the cookie-retry precondition), optionally recording every call."""

    def respond(url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        if record is not None:
            record.append(headers)
        if marker in headers.get("Cookie", ""):
            return (200, b"<html><body>cffi success</body></html>", "text/html")
        return DENIED

    return respond


__all__ = ["DENIED", "RENDERED", "cffi_retry_chain", "cookie_oracle"]
