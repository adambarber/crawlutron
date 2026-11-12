"""Anonymous IP-bucket metering (#668): the surfaces outside the auth layer.

The rate-limit law: every externally reachable surface is metered. Surfaces
with no caller credential meter per remote IP here (#659/#663 inheritance).
"""

from __future__ import annotations

import time

from fastapi.responses import JSONResponse

from crawlutron.service.meter_ledger import MeterLedger, register_shared_meter

__all__ = ["AnonymousThrottle", "Throttled", "shared_anonymous_throttle"]

#: The anonymous budget: these surfaces cost route matching plus envelope
#: construction -- no fetch, no egress. 120/60s per remote IP; no env knob.
MAX_REQUESTS_PER_WINDOW = 120
WINDOW_SECONDS = 60.0
#: The LRU bound: attacker IP rotation cannot grow memory unboundedly; a
#: quiescent bucket is the eviction victim and an active IP is never evicted.
BUCKET_CAPACITY = 1024
NO_CLIENT_BUCKET = "unknown"

#: The accepted 2x boundary burst: a bucket that hit exactly MAX admits one
#: more (count > MAX refuses), so a racer sees at most 2*MAX-1 successes.


class Throttled(Exception):
    """The meter's own typed refusal: no getattr dance at the refuse site.

    Field names avoid ``status`` (the job-store plane's reserved word).
    """

    def __init__(self, refusal_status: int, refusal_code: str, refusal_message: str) -> None:
        super().__init__(refusal_message)
        self.refusal_status = refusal_status
        self.refusal_code = refusal_code
        self.refusal_message = refusal_message


class _Bucket:
    """One remote IP's window ledger under the meter's lock."""

    __slots__ = ("count", "window_started")

    def __init__(self, now: float) -> None:
        self.count = 0
        self.window_started = now


class AnonymousThrottle(MeterLedger):
    """Per-remote-IP fixed-window meter over the unauthenticated surfaces."""

    def __init__(self, *, clock=time.monotonic) -> None:
        super().__init__(clock=clock)

    def check(self, remote: str | None) -> None:
        """Count one anonymous request for `remote`; refuse past the budget."""
        key = remote or NO_CLIENT_BUCKET
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is not None:
                self._buckets.move_to_end(key)
                if now - bucket.window_started > WINDOW_SECONDS:
                    bucket.count = 0
                    bucket.window_started = now
            else:
                bucket = self._evict_or_mint(now)
                self._buckets[key] = bucket
            bucket.count += 1
            if bucket.count > MAX_REQUESTS_PER_WINDOW:
                from crawlutron.runtime.metrics import get_metrics

                get_metrics().record_anonymous_throttle_refusal()
                raise Throttled(429, "anonymous_throttled", "Too many requests; retry later")

    def _evict_or_mint(self, now: float) -> _Bucket:
        """Mint a bucket, evicting the least-recently-used one at capacity."""
        while len(self._buckets) >= BUCKET_CAPACITY:
            self._buckets.popitem(last=False)
        return _Bucket(now)


_SHARED = AnonymousThrottle()


def shared_anonymous_throttle() -> AnonymousThrottle:
    """The one anonymous meter every unauthenticated surface uses."""
    return _SHARED


register_shared_meter(__name__, _SHARED)

#: The paths this meter gates: MCP anonymous traffic and FastAPI's static
#: docs pages; exempt GETs meter through the middleware (ANONYMOUS_METERED).
METERED_PREFIXES = ("/docs", "/redoc")
METERED_EXACT = ("/mcp",)
#: The operator-guarded surface: a credentialed scrape is the operator's caller,
#: never the anonymous bucket (the declared ANONYMOUS_METERED set omits it).
_OPERATOR_METERED_EXACT = ("/metrics",)


async def throttle_anonymous_surfaces(request, call_next):
    """HTTP middleware meter over every surface with no caller to key on.

    Registered OUTERMOST, counting at the REQUEST stage: /mcp and the operator
    /metrics scrape skip only when a credential is presented (#664/#914).
    """
    path = request.url.path
    credentialed = _presents_credential(request)
    operator_surface = path in _OPERATOR_METERED_EXACT
    if credentialed and (path in METERED_EXACT or operator_surface):
        return await call_next(request)
    try:
        shared_anonymous_throttle().check(_remote(request))
    except Throttled as throttled:
        return _refuse(path, throttled)
    return await call_next(request)


def _presents_credential(request) -> bool:
    """Whether the request carries any caller credential (header-level check)."""
    for name in ("x-api-key", "authorization"):
        value = request.headers.get(name, "").strip()
        if value:
            return True
    return False


def _remote(request) -> str | None:
    """The remote address string, or None when the transport provides none."""
    client = request.client
    return client.host if client else None


def _refuse(path: str, throttled: Throttled) -> JSONResponse:
    """The refusal in the surface's shape: JSON-RPC on /mcp, envelope elsewhere."""
    from crawlutron_mcp.constants import RATE_LIMITED
    from crawlutron_mcp.protocol import error_response

    from crawlutron_rest.api.exceptions import error_envelope

    if path in METERED_EXACT:
        content: object = error_response(None, RATE_LIMITED, throttled.refusal_message)
    else:
        content = error_envelope(throttled.refusal_code, throttled.refusal_message)
    return JSONResponse(status_code=throttled.refusal_status, content=content)
