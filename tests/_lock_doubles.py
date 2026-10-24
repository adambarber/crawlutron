"""Delegating lock doubles: observe one operation, inherit the rest. ``DelegatingLock``
is the forwarding tail written once, so a double overrides only the operation it
watches instead of copying the surface and risking a gap that hides a real-bug read.
"""

from __future__ import annotations

from typing import Literal, Protocol


class _LockLike(Protocol):
    def acquire(self, *args: object, **kwargs: object) -> object: ...
    def release(self) -> object: ...
    def locked(self) -> object: ...
    def wait(self, *args: object, **kwargs: object) -> object: ...
    def notify(self, *args: object) -> object: ...
    def notify_all(self) -> object: ...


class DelegatingLock:
    """Every lock operation forwarded to ``inner``; override what you observe, and
    stay installed over a live object rather than faking lock semantics -- the
    property under test is usually *which* lock was taken.
    """

    def __init__(self, inner: _LockLike) -> None:
        self._inner = inner

    def acquire(self, *args: object, **kwargs: object) -> object:
        return self._inner.acquire(*args, **kwargs)

    def release(self) -> object:
        return self._inner.release()

    def locked(self) -> object:
        return self._inner.locked()

    def wait(self, *args: object, **kwargs: object) -> object:
        return self._inner.wait(*args, **kwargs)

    def notify(self, *args: object) -> object:
        return self._inner.notify(*args)

    def notify_all(self) -> object:
        return self._inner.notify_all()

    def __enter__(self) -> DelegatingLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.release()
        return False
