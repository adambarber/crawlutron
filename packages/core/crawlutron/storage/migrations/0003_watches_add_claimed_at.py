from crawlutron.storage.migrations.ddl import add_column_step

__depends__ = {"0002_watches_add_owner"}

# The sweep-pass claim: one sweeper's CAS marks the pair; another skips the row
# until the stamp goes stale (at-least-once checks).
steps = [
    *add_column_step("watches", "claimed_at REAL"),
    *add_column_step("watches", "claimed_by TEXT"),
]
