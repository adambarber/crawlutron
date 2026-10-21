from app.storage.migrations.ddl import add_column_step

__depends__ = {"0003_crawl_jobs_split_accumulators"}

# owner="" is the shared anonymous bucket; historical rows backfill into
# it so they stay readable anonymously (compatibility contract).
steps = add_column_step("crawl_jobs", "owner TEXT NOT NULL DEFAULT ''")
