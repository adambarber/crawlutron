import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import job_runner
from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.api.jobs_router import router
from app.crawl.job_store import JobStore
from app.settings import CrawlerSettings


async def test_probe2(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: asyncio.Event()

    async def fake(payload_dict, *, settings, shutdown_event):
        print("  fake called", payload_dict.get("urls"), flush=True)
        urls = payload_dict.get("urls") or [payload_dict.get("url")]
        return {
            "results": [
                {
                    "original_url": u,
                    "normalized_url": u,
                    "final_url": u,
                    "status": "success",
                    "classification": "rendered",
                    "discovered_links": [],
                }
                for u in urls
            ],
            "submitted_count": len(urls),
        }

    job_runner.install_execute_crawl(fake)

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await client.post("/v1/jobs", json={"root_url": "http://example.test/a", "policy": {}})
    jid = r.json()["job_id"]
    for i in range(150):
        await asyncio.sleep(0.02)
        j = store.load(jid)
        if j.status.value in {"completed", "failed"}:
            print("DONE:", j.status.value, flush=True)
            return
    print("TIMEOUT", flush=True)
