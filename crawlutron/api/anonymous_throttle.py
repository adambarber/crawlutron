"""Anonymous IP-bucket metering (#668): the surfaces outside the auth layer.

The rate-limit law: every externally reachable surface is metered. The five
exempt GETs, POST /mcp anonymous traffic, and the 404 route-miss have no
caller credential to key a bucket on, so they meter per remote IP here.

#659/#663 inheritance: buckets are LRU-bounded, the clock is monotonic, and
the meter is registered process state (#621).
"""

from __future__ import annotations

import time

from fastapi.responses import JSONResponse

from crawlutron.api.meter_ledger import MeterLedger, register_shared_meter

__all__ = ["AnonymousThrottle", "shared_anonymous_throttle"]

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


def _throttled() -> Exception:
    """The caller-visible metering refusal: a 429-shaped HTTPException."""
    from crawlutron.api.exceptions import http_error

    return http_error(429, "anonymous_throttled", "Too many requests; retry later")


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
                raise _throttled()

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
#: pages. The 404 route-miss meters inside http_exception_handler (it has no
#: route object and must carry the discovery links there).
METERED_PREFIXES = ("/docs", "/redoc")
METERED_EXACT = ("/mcp",)


async def throttle_anonymous_surfaces(request, call_next):
    """HTTP middleware meter over the unauthenticated docs surfaces and /mcp.

    Presented-credential /mcp traffic is skipped: it resolves a caller identity
    inline and meters through the caller's own quota (the #664 layer); the
    anonymous bucket is for traffic with no caller to key on. A 404 whose
    response carries no matched route is a ROUTE MISS -- the 404 oracle -- and
    meters on the response stage like every other unguarded surface.
    """
    path = request.url.path
    if path in METERED_EXACT and _presents_credential(request):
        return await call_next(request)
    if path in METERED_EXACT or path.startswith(METERED_PREFIXES):
        try:
            shared_anonymous_throttle().check(_remote(request))
        except Exception as throttled:
            return _refuse(path, throttled)
    response = await call_next(request)
    if response.status_code == 404 and "route" not in request.scope:
        try:
            shared_anonymous_throttle().check(_remote(request))
        except Exception as throttled:
            return _refuse(path, throttled)
    return response


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


def _refuse(path: str, throttled: Exception) -> JSONResponse:
    """The refusal in the surface's shape: JSON-RPC on /mcp, envelope elsewhere."""
    from crawlutron.api.exceptions import error_envelope_from_detail
    from crawlutron.mcp.constants import RATE_LIMITED
    from crawlutron.mcp.protocol import error_response

    detail = getattr(throttled, "detail", None)
    status = getattr(throttled, "status_code", 429)
    if path in METERED_EXACT:
        content: object = error_response(None, RATE_LIMITED, "Too many requests; retry later")
    else:
        content = error_envelope_from_detail(detail, status)
    return JSONResponse(status_code=status, content=content)
