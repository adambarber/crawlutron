"""cffi doubles: a recording session and a stream-shaped response.

Shared by the cffi visibility and streaming tests; the queue shape matches the
production drain (`_drain_capped_stream`), so a double that passes here keeps
the drain contract honest.
"""

from __future__ import annotations

from curl_cffi.requests.models import STREAM_END


class _Queue:
    """Yields scripted items then wedges on a never-set Event (fast-sleep safe)."""

    def __init__(self, items):
        self._items = list(items)
        self._wedge = __import__("asyncio").Event()
        self.drained = False

    async def get(self):
        if self._items:
            await __import__("asyncio").sleep(0)
            return self._items.pop(0)
        self.drained = True
        await self._wedge.wait()


class RecordingSession:
    """Records each get()'s URL and the resolve table it was born with."""

    def __init__(self):
        self.calls: list[str] = []
        self.resolve_entry: list[str] | None = None
        self.response = CffiResponseDouble([])
        self.closed = False

    async def close(self):
        self.closed = True

    async def get(self, url, **kwargs):
        self.calls.append(url)
        return self.response


class CffiResponseDouble:
    """The stream shape production drains: status, headers, queue, aclose."""

    def __init__(self, items):
        self.status_code = 200
        self.headers = {}
        self.queue = _Queue(items)
        self.closed = False

    async def aclose(self):
        self.closed = True


def terminal_response() -> CffiResponseDouble:
    """A 200 whose stream ends immediately: the terminal-arm shape."""
    return CffiResponseDouble([STREAM_END])


__all__ = ["CffiResponseDouble", "RecordingSession", "terminal_response"]
