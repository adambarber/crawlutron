"""What one fetch attempt produced, whichever operator produced it.

The harness has to report an outcome across a process boundary, and the
production fetcher's own shape does not survive that trip on its own. It returns
``None`` for "this attempt produced nothing", and a bare ``None`` on a queue is
indistinguishable from a worker that died holding nothing. So the boundary has to
carry the reason too -- and it has to carry it as a type rather than as another
convention, because a boundary has more than one producer and every producer that
invents its own shape eventually has a reader that unpacks it wrong. Two such
producers existed here: one reporting ``("ok", 200)`` and one reporting a record
whose success arm was a copy of the status code, read by a caller that had to be
told which slot was real.

The success arm is the production result, verbatim. That is what makes the report
worth reading: ``outcome.status_code`` is the fetcher's own field rather than a
copy of it, so it cannot drift from the result the strategy then consumes, and a
reader can reach every other field the result carries instead of only the two the
reporter chose to copy.

The corresponding production declaration is ``HttpFetchResult | None``, the return
of ``app.transport.fetchers._cffi_fetch``. This type adds the one thing
production does not have: the reason, which exists only because a queue discards
an exception raised in the child.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.http_fetch_result import HttpFetchResult


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """The attempt produced no result; ``error`` names the class that ended it.

    A transport failure is contract-fine and a hang is not: any exception the real
    curl_cffi raises ends the fetch in bounded time, so the class name is recorded
    rather than re-raised and the caller decides what it meant.
    """

    error: str
    detail: str = ""


type FetchOutcome = HttpFetchResult | FetchFailed
