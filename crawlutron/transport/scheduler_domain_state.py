"""One domain's entire remembered pressure state, replacing nine parallel dicts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawlutron.transport.scheduler import TokenBucket

__all__ = ["DomainGate", "DomainState"]


class DomainGate:
    """The domain's concurrency gate: the semaphore plus its public occupancy.

    ``Semaphore._value`` is a CPython detail, so in-flight occupancy is
    tracked here explicitly (#577 F1: any holder or waiter keeps the record
    live; ``locked()`` alone misses a half-full gate). ``async with`` works
    unchanged by delegation.
    """

    def __init__(self, limit: int) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self.occupancy = 0

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()
        self.occupancy += 1
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        self.occupancy -= 1
        self._semaphore.release()


class DomainState:
    """Everything the scheduler remembers about one domain.

    A field is dropped only when it is inert; liveness is per-field (see
    ``live``), so an in-flight reservation or a half-climbed failure ladder
    survives a mid-run purge. The breaker math lives here with the counters
    it escalates; the scheduler keeps only the knobs. Every timestamp here is
    a reading of the scheduler's ONE injected monotonic clock, handed in as
    ``now`` (#580): no method of this class reads a clock of its own, so a
    second family cannot re-enter through a side door.
    """

    def __init__(self) -> None:
        # Monotonic fire-time reservation; None = the domain owes no spacing.
        self.next_fire: float | None = None
        # Monotonic cooldown expiry (Retry-After); stamped from the injected
        # clock at the setter, so NTP steps cannot bend it (#580).
        self.cooldown_until: float | None = None
        self.circuit_open_until: float | None = None
        self.failures = 0
        self.trips = 0
        # Robots Crawl-delay floor: never time-elapsed, purged only by liveness
        # of the whole record, bounded by the politeness ceiling at the setter.
        self.min_delay = 0.0
        self.ema: float | None = None
        self.last_seen: float | None = None
        self.pin: tuple[str, float] | None = None
        self.bucket: TokenBucket | None = None
        # Per-domain concurrency gate, scheduler-lifetime: one home for the
        # domain's pacing, never re-minted per batch. The scheduler's _state()
        # mints the record and sizes this gate from its knobs.
        self.gate = DomainGate(1)

    def observation_copy(self) -> DomainState:
        """A snapshot record: every field copied; only the LIVE gate is shared.

        The gate is a semaphore, not data -- it cannot be copied, and
        sharing keeps occupancy truthful. The bucket is copied too (token
        balance included), so consuming through a snapshot never drains the
        scheduler's real bucket. Writes to the copy never reach the record."""
        snapshot = DomainState()
        snapshot.next_fire = self.next_fire
        snapshot.cooldown_until = self.cooldown_until
        snapshot.circuit_open_until = self.circuit_open_until
        snapshot.failures = self.failures
        snapshot.trips = self.trips
        snapshot.min_delay = self.min_delay
        snapshot.ema = self.ema
        snapshot.last_seen = self.last_seen
        snapshot.pin = self.pin
        if self.bucket is None:
            snapshot.bucket = None
        else:
            # Deferred import: scheduler.py imports this module, so the bucket
            # constructor is only resolvable at call time, not import time.
            from crawlutron.transport.scheduler import TokenBucket

            copy = TokenBucket(self.bucket.rate, self.bucket.capacity)
            copy.tokens = self.bucket.tokens
            copy.last_refill = self.bucket.last_refill
            snapshot.bucket = copy
        snapshot.gate = self.gate
        return snapshot

    def live(
        self,
        *,
        now: float,
        window: float,
        pin_ttl: float,
    ) -> bool:
        """True while any field still owes the domain something.

        Every liveness rule in one place -- the hand-enumerated six-map relay
        this replaces forgot three maps; a fourth state field cannot.
        """
        if self.cooldown_until is not None and self.cooldown_until > now:
            return True
        if self.circuit_open_until is not None and self.circuit_open_until > now:
            return True
        if self.next_fire is not None and self.next_fire > now:
            return True
        if self.failures > 0 or self.trips > 0:
            return True
        if self.min_delay > 0:
            return True
        if self.pin is not None and now - self.pin[1] <= pin_ttl:
            return True
        if self.ema is not None and self.last_seen is not None and self.last_seen > now - window:
            return True
        # In use right now: any holder or waiter -- the gate's public
        # occupancy. ``locked()`` alone is "at capacity" and misses a half-full
        # gate (limit 2, one holder): a record dropped then re-mints a FULL
        # fresh gate under the old holder and the cap silently widens
        # (#577 adversarial F1). The count rides the gate itself (DomainGate),
        # so no semaphore implementation detail is read.
        if self.gate.occupancy > 0:
            return True
        # The bucket rides along: it is kept while the record is live for any
        # other reason, never a reason by itself (idle domains drop theirs).
        return False

    def charge_failure(self, *, now: float, threshold: int, base: float, cooldown_max: float) -> None:
        """Count one failure; open the breaker past ``threshold`` with an
        exponentially escalating cooldown (base, base*2, ... capped) -- a tarpit
        parks progressively longer. Total observations charged, not URLs: a
        requeued retry charges again.
        """
        self.failures += 1
        n = self.failures // threshold
        if n >= 1 and n > self.trips:
            self.trips = n
            self.circuit_open_until = now + min(base * (2 ** (n - 1)), cooldown_max)

    def reset_breaker(self) -> None:
        """Clear the failure ladder -- counters only. An open window is NOT
        cleared: a success during another job's park must not end the breaker
        (the pre-#577 maps did this by only zeroing the counters; a success
        while a peer parks is common, and ending the trip would wave the whole
        batch at the origin). Only a served cooldown half-opens the window."""
        self.failures = 0
        self.trips = 0

    def breaker_open(self, *, now: float) -> bool:
        """Whether the breaker window still stands; a served cooldown half-opens."""
        expiry = self.circuit_open_until
        if expiry is None:
            return False
        if now >= expiry:
            # Half-open: reset so a recovered domain is not penalised by a
            # stale escalation.
            self.failures = 0
            self.trips = 0
            self.circuit_open_until = None
            return False
        return True

    def breaker_remaining(self, *, now: float) -> float:
        """Seconds left on an open window, 0 when closed."""
        expiry = self.circuit_open_until
        if expiry is None:
            return 0.0
        return max(0.0, expiry - now)

    def feed_latency(self, latency_ms: float, *, smoothing: float, clock_now: float) -> None:
        """Fold one latency observation into the EMA (negative is a bad
        measurement and is rejected upstream of the call)."""
        alpha = smoothing
        self.ema = latency_ms if self.ema is None else self.ema * (1.0 - alpha) + latency_ms * alpha
        self.last_seen = clock_now

    def get_pin(self, *, clock_now: float, ttl: float) -> str | None:
        """The pinned strategy, or None once the TTL has elapsed (dropped, so
        the domain re-climbs the ladder)."""
        pin = self.pin
        if pin is None:
            return None
        if clock_now - pin[1] > ttl:
            self.pin = None
            return None
        return pin[0]

    def set_pin(self, strategy: str, *, clock_now: float) -> None:
        self.pin = (strategy, clock_now)
