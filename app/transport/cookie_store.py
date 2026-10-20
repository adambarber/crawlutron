"""One cookie-store concept and its two media: in-memory LRU and SQLite (SharedCookieStore)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class CookieStore(Protocol):
    """A per-namespace cookie store: read+touch, union write, clear."""

    def load(self, namespace: str, host: str) -> dict[str, str]: ...

    def merge(self, namespace: str, host: str, cookies: Mapping[str, str]) -> None: ...

    def clear(self, namespace: str = "") -> None: ...


class InMemoryCookieStore:
    """The process-local cookie medium: per-namespace LRU, thread-safe."""

    __slots__ = ("_data", "_lock", "_max")

    def __init__(self, *, max_hosts_per_namespace: int) -> None:
        self._max = max(1, int(max_hosts_per_namespace))
        self._data: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def load(self, namespace: str, host: str) -> dict[str, str]:
        key = (namespace or "", host)
        with self._lock:
            cookies = dict(self._data.get(key, {}))
            if key in self._data:
                self._data.move_to_end(key)
            return cookies

    def merge(self, namespace: str, host: str, cookies: Mapping[str, str]) -> None:
        key = (namespace or "", host)
        with self._lock:
            self._data.setdefault(key, {}).update(cookies)
            self._data.move_to_end(key)
            self._evict(key[0])

    def put(self, namespace: str, host: str, value: Mapping[str, str]) -> None:
        """Replace the slot wholesale; a union would merge stale keys.
        Re-storing a known host updates it in place without touching recency, matching
        the plain-mapping semantics the browser path always had (no move-to-end).
        """
        key = (namespace or "", host)
        with self._lock:
            self._data[key] = dict(value)
            self._evict(key[0])

    def clear(self, namespace: str = "") -> None:
        with self._lock:
            if namespace:
                for key in [k for k in self._data if k[0] == namespace]:
                    del self._data[key]
            else:
                self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def snapshot(self) -> dict[tuple[str, str], dict[str, str]]:
        """A copied view of every slot, consistent under the store's lock.
        The export/legacy path reads whole namespaces; a copy avoids holding the lock.
        """
        with self._lock:
            return {key: dict(value) for key, value in self._data.items()}

    def _evict(self, namespace: str) -> None:
        stale = [key for key in self._data if key[0] == namespace]
        over = len(stale) - self._max
        for key in stale[: max(0, over)]:
            del self._data[key]
