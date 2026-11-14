# crawlutron-mcp

MCP protocol and handler library for crawlutron’s Model Context Protocol
(MCP 2026-07-28) surface.

This package provides protocol types, request validation, the tool registry,
and JSON-RPC request handling used by the crawlutron REST service’s stateless
HTTP `POST /mcp` endpoint. It is consumed by `crawlutron-rest` as a library
(not a standalone stdio MCP server and not a CLI).

To use crawlutron over MCP, run the crawlutron REST service and connect an MCP
client to its HTTP `/mcp` endpoint. Crawl authorization is handled by the REST
service.

## License

MIT. See the repository's `LICENSE`.