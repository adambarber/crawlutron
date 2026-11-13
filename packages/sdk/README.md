# crawlutron-sdk

The Python client for a crawlutron service: async and sync clients, typed models,
and error mapping.

The SDK is deliberately a thin, curated client. It mirrors the served OpenAPI
schema by hand rather than importing `crawlutron`, so it stays installable beside
any application and carries no crawler dependency.

```python
from crawlutron_sdk import AsyncClient

async with AsyncClient("https://crawl.example.com", api_key="...") as client:
    capabilities = await client.capabilities()
```

## License

MIT. See the repository's `LICENSE`.