"""Test helper package.

Shared test factories (``build_payload``, ``run_crawl_async``,
``crawl_homepage_async``, ``make_content_cache``, ``make_content_cache_with_entry``,
``make_job_store``, ``run_job_control_test``, ``browser_available``,
``LLM_FORMAT_TEST_HTML``) used to live here; they have been consolidated into
:mod:`_testkit`, the single home for test factories. Import them from there.

This package remains so :mod:`helpers.server` (the fixture HTTP server) stays
importable as a submodule.
"""
