"""Typed domain refusals the service and search layers raise; the HTTP and MCP
doors map them to their own error shapes (#849). The base carries the wire
triplet verbatim — one exception family, no envelope construction below api.
"""

from __future__ import annotations

from fastapi import HTTPException

from crawlutron.models.json_value import JsonValue

__all__ = [
    "AuthRefusal",
    "BROWSER_POOL_MESSAGE",
    "BrowserPoolUnavailable",
    "CapacityExceeded",
    "CookieNamespaceRefusal",
    "DomainError",
    "JobNotFound",
    "JobStateConflict",
    "KeyVerifyThrottled",
    "QuotaRefusal",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "ValidationRefusal",
    "WatchLimitExceeded",
    "WatchNotFound",
    "is_quota_refusal",
]

#: The canonical 503 pool-down message render endpoints and the crawl executor share.
BROWSER_POOL_MESSAGE = "Browser pool is not initialized; the service is starting up or shutting down."


class DomainError(Exception):
    """One service refusal: status, code, message, details ride the instance."""

    def __init__(self, status: int, code: str, message: str, details: JsonValue | None = None) -> None:
        super().__init__(message)
        self.http_status = status
        self.code = code
        self.message = message
        self.details = details

    @property
    def status(self) -> int:
        """The HTTP status the doors map this refusal to."""
        return self.http_status


class ValidationRefusal(DomainError):
    """A 422 parameterized-triplet carrier: code/message/details at the raise site."""

    def __init__(
        self, code: str, message: str, details: JsonValue | None = None, *, status: int = 422
    ) -> None:
        super().__init__(status, code, message, details)


class WatchNotFound(DomainError):
    def __init__(self, watch_id: str) -> None:
        super().__init__(404, "not_found", f"Unknown watch_id: {watch_id}")


class WatchLimitExceeded(DomainError):
    def __init__(self, limit: int) -> None:
        super().__init__(429, "watch_limit_exceeded", f"Caller watch limit is {limit}")


class CapacityExceeded(DomainError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "capacity_exceeded",
            "Request refused: active requests are at the configured cap (CRAWLUTRON_MAX_ACTIVE_REQUESTS)",
        )


class KeyVerifyThrottled(DomainError):
    def __init__(self) -> None:
        super().__init__(429, "key_verify_throttled", "Too many failed key verifications; retry later")


class JobNotFound(DomainError):
    def __init__(self, job_id: str) -> None:
        super().__init__(404, "not_found", f"Unknown job_id: {job_id}")


class JobStateConflict(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "invalid_state", message)


class BrowserPoolUnavailable(DomainError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(503, "browser_pool_unavailable", message or BROWSER_POOL_MESSAGE)


class SearchProviderUnavailable(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "search_provider_unavailable", message)


class SearchProviderError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(502, "search_provider_error", message)


class AuthRefusal(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(401, code, message)


class CookieNamespaceRefusal(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(422, "anonymous_cookie_namespace", message)


class QuotaRefusal(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(429, "quota_exceeded", message)


def is_quota_refusal(exc: object) -> bool:
    """Whether ``exc`` is a caller-quota refusal (429 quota_exceeded): the caller
    is at their own page/concurrency budget, so the guarded work never ran.
    """
    if isinstance(exc, QuotaRefusal):
        return True
    if not isinstance(exc, HTTPException):
        return False
    detail = getattr(exc, "detail", None)
    return (
        exc.status_code == 429
        and isinstance(detail, dict)
        and detail.get("code") == "quota_exceeded"
        and "message" in detail
    )
