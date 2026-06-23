from __future__ import annotations

from typing import Any

from .browser import BrowserRenderer
from .models import CrawlJob
from .service import render_request
from .settings import CrawlerSettings

try:
    from fastapi import FastAPI, HTTPException
except ImportError:  # pragma: no cover - imported in container runtime
    FastAPI = None  # type: ignore[assignment]
    HTTPException = Exception  # type: ignore[assignment]


settings = CrawlerSettings.from_mapping()
renderer = BrowserRenderer()

if FastAPI is not None:
    app = FastAPI(title="Browser Crawler Service")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/render")
    async def render(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await render_request(payload, settings=settings, worker=lambda job: renderer.render(job.url, job.options))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
else:
    app = None
