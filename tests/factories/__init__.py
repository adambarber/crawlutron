"""The construction surface: per-domain factory doors plus the ``Factory`` handle."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from crawlutron.storage import caller_key_store, job_store, result_store
from crawlutron.transport.cookie_jar import CookieJar
from tests._app_factory import service_app
from tests._policy_factories import job_store as make_store
from tests.factories.client import isolation_client


class Factory:
    """One handle over the package for the ``factory`` fixture."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def app(self) -> FastAPI:
        """The module-level ASGI app, proven present."""
        return service_app()

    def db(self, name: str = "jobs.db") -> job_store.JobStore:
        """A ``JobStore`` bound to a per-test SQLite database."""
        return make_store(self._tmp_path / name)

    def client(
        self,
    ) -> AbstractAsyncContextManager[
        tuple[AsyncClient, job_store.JobStore, result_store.CrawlResultStore, caller_key_store.CallerKeyStore]
    ]:
        """An HTTP client over isolated stores; yields the client and the stores."""
        return isolation_client(self._tmp_path)

    def cookie_jar(self) -> CookieJar:
        """A fresh jar; process-global jar state resets around every test."""
        return CookieJar()
