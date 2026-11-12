from crawlutron.storage.migrations.ddl import add_column_step

__depends__ = {"0001_watches_initial"}

# owner="" is the shared anonymous bucket; historical rows backfill into
# it so they stay readable anonymously (compatibility contract).
steps = add_column_step("watches", "owner TEXT NOT NULL DEFAULT ''")
