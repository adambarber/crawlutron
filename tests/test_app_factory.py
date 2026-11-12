"""The create_app factory owns store construction, logging and lifespan (#A1)."""

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI

from crawlutron.config.settings import CrawlerSettings


def test_create_app_returns_a_wired_app():
    from crawlutron_rest.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.state.crawler_settings is not None
    assert app.state.job_store is not None
    assert app.state.result_store is not None
    assert app.state.watch_store is not None
    assert app.state.caller_key_store is not None
    assert not hasattr(app.state, "key_admin_token"), "ONE home: the settings object (#666)"
    assert app.router.lifespan_context is not None


def test_create_app_accepts_settings():
    from crawlutron_rest.main import create_app

    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_KEY_ADMIN_TOKEN": "secret"})
    app = create_app(settings)
    assert app.state.crawler_settings is settings
    assert app.state.crawler_settings.key_admin_token == "secret"


def test_import_configures_nothing(tmp_path):
    """Importing crawlutron_rest.main is side-effect free: no database file appears in
    the working directory and no service logging handler is installed."""
    import os

    import crawlutron_rest.main

    # Pin THIS tree so the probe observes the factory under test, not another worktree:
    # the member roots rest imports (core, and mcp since #917).
    rest_root = Path(crawlutron_rest.main.__file__).resolve().parents[1]
    packages = rest_root.parent.parent
    core_root = packages / "core" / "src"
    mcp_root = packages / "mcp" / "src"
    probe = (
        "import logging, os, sys\n"
        "import crawlutron_rest.main\n"
        "made = sorted(set(os.listdir('.')) - {'__pycache__'})\n"
        "db = [p for p in os.listdir('cache') if p.endswith('.db')] if os.path.isdir('cache') else []\n"
        "handlers = [type(h).__name__ for h in logging.getLogger().handlers]\n"
        "assert crawlutron_rest.main.__file__ == sys.argv[1], crawlutron_rest.main.__file__\n"
        "print('MADE', made)\n"
        "print('DBS', db)\n"
        "print('HANDLERS', handlers)\n"
    )
    env = {
        k: v
        for k, v in {**os.environ, "PYTHONPATH": f"{str(rest_root)}:{str(core_root)}:{str(mcp_root)}"}.items()
        if not k.startswith("CRAWLUTRON_")
    }
    result = subprocess.run(
        [sys.executable, "-c", probe, str(crawlutron_rest.main.__file__)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        env=env,
    )
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert lines["MADE"] == "[]", f"import created filesystem entries: {lines['MADE']}"
    assert lines["DBS"] == "[]", "import created a database file"
    assert "StreamHandler" not in lines["HANDLERS"], lines["HANDLERS"]


def test_module_app_is_lazy_but_available():
    """`crawlutron_rest.main.app` still resolves (ASGI discovery, tests) and is a real app."""
    import crawlutron_rest.main as main_mod

    app = main_mod.app
    assert isinstance(app, FastAPI)
    assert main_mod.app is app, "module-level app must be built once, not per access"
