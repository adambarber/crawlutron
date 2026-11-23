"""Output rendering: print service bodies faithfully per output mode."""

from __future__ import annotations

import json
import sys

__all__ = ["write_body"]


def write_body(body: bytes, output: str = "json", pretty: bool = False) -> None:
    """Write the service body to stdout, faithfully per ``output`` mode.

    ``json`` bodies are optionally pretty-printed; every other mode is echoed
    byte-for-byte. A trailing newline is added only when the body lacks one.
    """
    text = body.decode("utf-8", errors="replace")
    if output == "json" and pretty:
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            pass
    if not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)
