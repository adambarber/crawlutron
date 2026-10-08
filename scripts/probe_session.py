"""Ad-hoc browser/curl_cffi probe for an arbitrary public site (loopback by default).

Originally hardcoded to ``/opt/stack/crawlutron`` and
``/opt/stack/shared/python`` for an in-house container; rewritten to derive
those paths from the script location and ``CRAWLUTRON_*`` env vars so
the probe runs in any clone of the repository, with no absolute path
from the deployment it was written for.

Run from the repository root:

    python3 scripts/probe_session.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Resolve the crawlutron root from this script location so the probe
# works whether it is invoked from a container, a workstation, or a CI
# checkout. The previous version of this script also injected the parent
# monorepo's ``shared/python`` directory; the equivalent logging helper now
# lives at ``app.runtime.logging_config`` and is reachable from the repo root.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent  # the repository root, which is now the service root

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROBE_TARGET = os.environ.get("CRAWLUTRON_PROBE_TARGET", "https://example.com/")
_PROBE_OUTPUT = os.environ.get("CRAWLUTRON_PROBE_OUTPUT", str(_REPO_ROOT / "tmp" / "probe_out.txt"))


async def main() -> None:
    out = []
    from app.transport.browser_driver import BrowserDriver

    # open_page, not acquire-then-new_page: a caller that wants a page has no
    # reason to know the browser is recycled underneath it. Reaching past that
    # for `_acquire_browser` is how this probe stopped running when the
    # lifecycle split moved the method -- a script nobody executes fails
    # silently, and "silently" here meant "AttributeError on the first run".
    driver = BrowserDriver()
    page = await driver.open_page({})
    await page.goto(_PROBE_TARGET, wait_until="domcontentloaded", timeout=45000)
    ua = await page.evaluate("navigator.userAgent")
    cookies = await page.context.cookies(_PROBE_TARGET)
    out.append(f"UA: {ua[:60]}...")
    out.append(f"cookies: {[c['name'] for c in cookies]}")

    import curl_cffi.requests as cr

    async with cr.AsyncSession(impersonate="firefox135") as client:
        jar = {c["name"]: c["value"] for c in cookies}
        # Each probe adds exactly one thing to the request the one before it made,
        # so the three status codes differ only by the header under test. Spelled
        # out as three calls they were three copies of the same request to keep in
        # step; as a ladder the progression is the thing being read.
        for label, extra in (
            ("cffi plain", {}),
            ("cffi + cookies", {"cookies": jar}),
            ("cffi + cookies + UA", {"cookies": jar, "headers": {"User-Agent": ua}}),
        ):
            response = await client.get(_PROBE_TARGET, allow_redirects=False, **extra)
            out.append(f"{label}: {response.status_code}")
    await page.close()
    out_path = Path(_PROBE_OUTPUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out))


asyncio.run(main())
