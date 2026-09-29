"""Delegating lock doubles: observe one operation, inherit the rest.

A lock's surface is large -- ``acquire`` takes a timeout and a blocking flag,
``wait`` takes a timeout, and there are three release-ish calls. Two modules
each spelled out a subclass whose only real content was the one operation it
observed, and the rest was the same seven-method forwarding tail copied along
with it. That tail is the duplication, and it is also a trap: a double that is
missing a method the real lock has fails with an ``AttributeError`` in whichever
test happens to be the first to call it, which reads as a bug in the code
under test rather than in the double.

``DelegatingLock`` is that tail, written once. A double subclasses it and
overrides only the operation it is watching, calling ``super()`` for the rest.
"""

from __future__ import annotations

from typing import Any, Literal


class DelegatingLock:
    """Every lock operation forwarded to ``inner``; override what you observe.

    ``inner`` is whatever real lock is being stood in for, so the double is
    always installed over a live object rather than faking lock semantics --
    the property under test is usually *which* lock was taken, not whether
    locking works.
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
