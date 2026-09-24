"""Test helper package.

Shared test factories (``build_payload``, ``run_crawl_async``,
``crawl_homepage_async``, ``make_content_cache``, ``make_content_cache_with_entry``,
``make_job_store``, ``run_job_control_test``, ``browser_available``,
``LLM_FORMAT_TEST_HTML``) used to live here; they now live in focused modules
under ``tests``. Import each helper from the module that owns it.

This package remains so :mod:`helpers.server` (the fixture HTTP server) stays
importable as a submodule.
"""
