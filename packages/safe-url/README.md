# crawlutron-safe-url

A URL type that *is* SSRF-safe admission.

Any service that accepts a URL -- a link shortener, a webhook receiver, a URL
expander, a social unfurler, a browser driver, an LLM tool -- needs "can I safely
fetch this?" answered by a constructor rather than by a hand-rolled
`urlparse` + `ipaddress` check. `NormalizedUrl` does that: host policy, IDNA
canonicalization, registrable domain, private-target guard, and credential-safe
redaction, in one admission step.

```python
from safe_url import NormalizedUrl, HostPolicy

policy = HostPolicy.from_strings("example.com,*.trusted.example", "*.ads.example")
url = NormalizedUrl("HTTPS://Example.COM:443/a/b/?utm_source=x#frag", policy=policy)
url.raw  # 'https://example.com/a/b/'
url.host  # 'example.com'
url.site  # 'example.com'
```

A URL that fails admission is never constructed, so there is no later "did I check
this?" -- the type is the check. The package imports nothing from `crawlutron`:
`crawlutron-core` depends on it, never the reverse.

Install with the public-suffix dataset for registrable-domain lookups:
`pip install "crawlutron-safe-url[psl]"`.

## License

MIT. See the repository's `LICENSE`.