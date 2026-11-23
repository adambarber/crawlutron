"""Fence job writes with a generation so a late save cannot wipe a terminal row."""

from crawlutron.storage.migrations.ddl import add_column_step

__depends__ = {"0007_crawl_jobs_add_runner_claim"}

steps = add_column_step("crawl_jobs", "generation INTEGER NOT NULL DEFAULT 0")
