"""Ad-hoc browser/curl_cffi probe for an arbitrary public site (loopback by default).

Originally hardcoded to ``/opt/stack/browser-crawler`` and
``/opt/stack/shared/python`` for an in-house container; rewritten to derive
those paths from the script location and ``BROWSER_CRAWLER_*`` env vars so
the probe runs in any clone of the repository (adversarial review
F-probe-session-hardcoded-paths).

Run from the docker/browser-crawler directory:

    python3 scripts/probe_session.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Resolve the browser-crawler root from this script location so the probe
# works whether it is invoked from a container, a workstation, or a CI
# checkout. The previous version of this script also injected the parent
# monorepo's ``shared/python`` directory; the equivalent logging helper now
# lives at ``app.logging_config`` and is reachable from the repo root.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent  # docker/browser-crawler

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROBE_TARGET = os.environ.get(
    "BROWSER_CRAWLER_PROBE_TARGET", "https://example.com/"
)
_PROBE_OUTPUT = os.environ.get(
    "BROWSER_CRAWLER_PROBE_OUTPUT", str(_REPO_ROOT / "tmp" / "probe_out.txt")
)


async def main() -> None:
    out = []
    from app.crawl.strategies.browser import BrowserRenderer

    r = BrowserRenderer(warmup_urls=[])
    browser = await r._acquire_browser(type("O", (), {"browser_profile": {}, "max_retries": 0})())
    page = await browser.new_page()
    await page.goto(_PROBE_TARGET, wait_until="domcontentloaded", timeout=45000)
    ua = await page.evaluate("navigator.userAgent")
    cookies = await page.context.cookies(_PROBE_TARGET)
    out.append(f"UA: {ua[:60]}...")
    out.append(f"cookies: {[c['name'] for c in cookies]}")

    import curl_cffi.requests as cr

    async with cr.AsyncClient(impersonate="chrome124") as client:
        r1 = await client.get(_PROBE_TARGET, allow_redirects=False)
        out.append(f"cffi plain: {r1.status_code}")
        r2 = await client.get(
            _PROBE_TARGET,
            allow_redirects=False,
            cookies={c["name"]: c["value"] for c in cookies},
        )
        out.append(f"cffi + cookies: {r2.status_code}")
        r3 = await client.get(
            _PROBE_TARGET,
            allow_redirects=False,
            cookies={c["name"]: c["value"] for c in cookies},
            headers={"User-Agent": ua},
        )
        out.append(f"cffi + cookies + UA: {r3.status_code}")
    await page.close()
    out_path = Path(_PROBE_OUTPUT)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out))


asyncio.run(main())
