"""Delegating lock doubles: observe one operation, inherit the rest.

``DelegatingLock`` is the forwarding tail written once, so a double overrides
only the operation it watches instead of copying seven methods and risking a
gap that reads as a bug in the code under test rather than in itself.
"""

from __future__ import annotations

from typing import Any, Literal


class DelegatingLock:
    """Every lock operation forwarded to ``inner``; override what you observe, and
    stay installed over a live object rather than faking lock semantics -- the
    property under test is usually *which* lock was taken.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.acquire(*args, **kwargs)

    def release(self) -> Any:
        return self._inner.release()

    def locked(self) -> Any:
        return self._inner.locked()

    def wait(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.wait(*args, **kwargs)

    def notify(self, *args: Any) -> Any:
        return self._inner.notify(*args)

    def notify_all(self) -> Any:
        return self._inner.notify_all()

    def __enter__(self) -> DelegatingLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.release()
        return False
