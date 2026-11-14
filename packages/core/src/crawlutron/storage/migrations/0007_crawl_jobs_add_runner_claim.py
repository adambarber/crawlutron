from crawlutron.storage.migrations.ddl import add_column_step

__depends__ = {"0006_crawl_jobs_split_frontier"}

# The runner lease: one worker's claim on a RUNNING row; recovery rewinds only
# rows that are unclaimed or whose claim is stale.
steps = [
    *add_column_step("crawl_jobs", "claimed_by TEXT"),
    *add_column_step("crawl_jobs", "claimed_at REAL"),
]
