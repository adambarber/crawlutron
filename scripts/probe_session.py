import asyncio, sys
sys.path.insert(0, "/opt/stack/browser-crawler")
sys.path.insert(0, "/opt/stack/shared/python")

async def main():
    out = []
    from app.crawl.strategies.browser import BrowserRenderer
    r = BrowserRenderer(warmup_urls=[])
    browser = await r._acquire_browser(type("O", (), {"browser_profile": {}, "max_retries": 0})())
    page = await browser.new_page()
    await page.goto("https://www.worcesterma.gov/", wait_until="domcontentloaded", timeout=45000)
    ua = await page.evaluate("navigator.userAgent")
    cookies = await page.context.cookies("https://www.worcesterma.gov/")
    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    out.append(f"UA: {ua[:60]}...")
    out.append(f"cookies: {[c['name'] for c in cookies]}")

    # Now try curl_cffi three ways
    import curl_cffi.requests as cr
    async with cr.AsyncClient(impersonate="chrome124") as client:
        # 1. plain
        r1 = await client.get("https://www.worcesterma.gov/", allow_redirects=False)
        out.append(f"cffi plain: {r1.status_code}")
        # 2. cookies only
        r2 = await client.get("https://www.worcesterma.gov/", allow_redirects=False,
                              cookies={c["name"]: c["value"] for c in cookies})
        out.append(f"cffi + cookies: {r2.status_code}")
        # 3. cookies + browser UA
        r3 = await client.get("https://www.worcesterma.gov/", allow_redirects=False,
                              cookies={c["name"]: c["value"] for c in cookies},
                              headers={"User-Agent": ua})
        out.append(f"cffi + cookies + UA: {r3.status_code}")
    await page.close()
    open("/opt/stack/browser-crawler/tmp/probe_out.txt", "w").write("\n".join(out))

asyncio.run(main())
