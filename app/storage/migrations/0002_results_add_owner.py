from app.storage.migrations.ddl import add_column_step

__depends__ = {"0001_results_initial"}

# owner="" is the shared anonymous bucket; historical rows backfill into
# it so they stay readable anonymously (compatibility contract).
steps = add_column_step("results", "owner TEXT NOT NULL DEFAULT ''")
