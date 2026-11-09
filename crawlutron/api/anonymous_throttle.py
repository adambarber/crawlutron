"""Anonymous IP-bucket metering (#668): the surfaces outside the auth layer.

The rate-limit law: every externally reachable surface is metered. The exempt
GETs (discovery, docs, health), POST /mcp anonymous traffic, and the route-miss
oracle have no caller credential to key a bucket on, so they meter per remote
IP here.

#659/#663 inheritance: buckets are LRU-bounded, the clock is monotonic, and
the meter is registered process state (#621).
"""

from __future__ import annotations

import time

from fastapi.responses import JSONResponse

from crawlutron.api.meter_ledger import MeterLedger, register_shared_meter

__all__ = ["AnonymousThrottle", "Throttled", "shared_anonymous_throttle"]

#: The anonymous budget: these surfaces cost route matching plus envelope
#: construction (and, on /mcp, the pure tools/list arm) -- no fetch, no egress.
#: 120 requests per 60s per remote IP is the defensible default; no env knob
#: (the knob-contract gate carries no row for this surface).
MAX_REQUESTS_PER_WINDOW = 120
WINDOW_SECONDS = 60.0
#: The LRU bound: attacker IP rotation cannot grow memory unboundedly; the
#: least-recently-used (quiescent for a full window's worth of fresher callers)
#: bucket is the eviction victim. An ACTIVE ip is touched on every check and
#: sits at the MRU end, so eviction never resets an in-window count.
BUCKET_CAPACITY = 1024
NO_CLIENT_BUCKET = "unknown"

#: The accepted 2x boundary burst: a bucket that hit exactly MAX within the
#: window admits one more request (count > MAX refuses), so a client racing
#: the boundary sees at most 2*MAX-1 successes before steady 429s.


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

#: The paths this meter gates: MCP anonymous traffic and FastAPI's static docs
#: pages. The exempt GETs (health, ready, discovery, capabilities, agent-hints,
#: openapi.json) meter through ANONYMOUS_METERED's route-driven gate below.
METERED_PREFIXES = ("/docs", "/redoc")
METERED_EXACT = ("/mcp",)


async def throttle_anonymous_surfaces(request, call_next):
    """HTTP middleware meter over every surface with no caller to key on.

    Registered OUTERMOST (last) and counting at the REQUEST stage: every
    request consumes exactly ONE anonymous unit, including 413 body-size and
    403 origin rejections -- REJECTED requests are still work (the pinning
    probes prove one unit each from a MAX-1 baseline). One stage means one
    count per request: no double-metering anywhere.

    Presented-credential /mcp traffic is skipped: it resolves a caller identity
    inline and meters through the caller's own quota (the #664 layer). Caller
    semantics for everything else: the caller plane has no request-count quota
    (page/concurrency only), so requests to caller-guarded routes consume the
    anonymous IP bucket too -- one request, one unit, whatever the response.
    """
    path = request.url.path
    if path in METERED_EXACT and _presents_credential(request):
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
    from crawlutron.api.exceptions import error_envelope
    from crawlutron.mcp.constants import RATE_LIMITED
    from crawlutron.mcp.protocol import error_response

    if path in METERED_EXACT:
        content: object = error_response(None, RATE_LIMITED, throttled.refusal_message)
    else:
        content = error_envelope(throttled.refusal_code, throttled.refusal_message)
    return JSONResponse(status_code=throttled.refusal_status, content=content)
