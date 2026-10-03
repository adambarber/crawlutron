175 modules, 610 internal edges

FAN-IN (hubs -- every one is a caller to update when this module changes)
   43  app.constants
   33  app.models.host_policy
   19  app.models.crawl_policy
   17  app.settings
   16  app.models.normalized_url
   14  app.models.crawl_result
   14  app.models.engine_crawl_result
   14  app.models.taxonomy
   12  app.crawl.policy
   12  app.env
   12  app.validation
   11  app.crawl.migrations.manifest
   10  app.crawl.job_store
   10  app.crawl.result_helpers
    9  app.models.crawl_extraction_method

FAN-OUT (reaching widely -- candidates for a narrower dependency)
   26  app.crawl.request_pipeline
   21  app.crawl.engine
   20  app.crawl.strategies.http
   19  app.api.response_shaping
   18  app.api.crawl_endpoint
   17  app.api.render
   16  app.crawl.strategies.browser
   16  app.crawl.strategies.browser_result
   16  app.crawl.strategy_chain
   14  app.crawl.admission

CYCLES: 3
  app.crawl.sqlite_store -> app.crawl.migrations.manifest -> app.crawl.sqlite_store
  app.crawl.extraction -> app.crawl.llm_format -> app.crawl.extraction
  app.safe_url -> app.crawl.strategies.pinned_transport -> app.safe_url
