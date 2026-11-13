# crawlutron-rest

The HTTP service surface for crawlutron: the FastAPI application, the REST routes,
MCP tool handlers, and the CLI.

The service is a stateless execution surface. Callers own frontier queueing,
deduplication, persistence, and host-policy decisions; nothing here keeps caller
state.

```sh
crawlutron serve --host 0.0.0.0 --port 8000
```

Discovery: `GET /.well-known/crawler-service` and `GET /v1/capabilities`.

## License

MIT. See the repository's `LICENSE`.