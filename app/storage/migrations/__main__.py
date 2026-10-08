"""Command-line entry point for crawlutron store migrations."""

from app.storage.migrations.runner import main

if __name__ == "__main__":
    main()
