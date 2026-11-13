# crawlutron-mcp

MCP server for crawlutron: exposes the crawl service as Model Context Protocol
tools, so an agent can discover capabilities and drive a crawl.

```sh
crawlutron-mcp
```

The server speaks MCP over stdio, so it needs no ports and no daemon. Point a
client at the command above and it starts on demand.

## License

MIT. See the repository's `LICENSE`.