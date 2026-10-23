"""Ad-hoc browser/curl_cffi probe for a public site (loopback by default).

Script-only, import-safe (#286): the browser launch and network request run
only under ``__main__``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Resolve the repo root from this script location; works from container,
# workstation, or CI checkout.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROBE_TARGET = os.environ.get("CRAWLUTRON_PROBE_TARGET", "https://example.com/")
_PROBE_OUTPUT = os.environ.get("CRAWLUTRON_PROBE_OUTPUT", str(_REPO_ROOT / "tmp" / "probe_out.txt"))


async def _run() -> None:
    out = []
    from app.transport.browser_driver import BrowserDriver

    driver = BrowserDriver()
    try:
        # open_page, not acquire-then-new_page: a caller that wants a page has no
        # reason to know the browser is recycled underneath it.
        page = await driver.open_page({})
        await page.goto(_PROBE_TARGET, wait_until="domcontentloaded", timeout=45000)
        ua = await page.evaluate("navigator.userAgent")
        cookies = await page.context.cookies(_PROBE_TARGET)
        out.append(f"UA: {ua[:60]}...")
        out.append(f"cookies: {[c['name'] for c in cookies]}")

        import curl_cffi.requests as cr

        async with cr.AsyncSession(impersonate="firefox135") as client:
            jar = {c["name"]: c["value"] for c in cookies}
            # Each probe adds exactly one thing to the request the one before it
            # made, so the three status codes differ only by the header under test.
            for label, extra in (
                ("cffi plain", {}),
                ("cffi + cookies", {"cookies": jar}),
                ("cffi + cookies + UA", {"cookies": jar, "headers": {"User-Agent": ua}}),
            ):
                response = await client.get(_PROBE_TARGET, allow_redirects=False, **extra)
                out.append(f"{label}: {response.status_code}")
        await page.close()
    finally:
        await driver.close()
    out_path = Path(_PROBE_OUTPUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out))


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
