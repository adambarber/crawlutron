# crawlutron-core

The crawler engine: URL admission, fetch and transport, extraction, orchestration,
storage, and the `admission` resource-adaptive admission kernel.

This is a library, not a service. To run the HTTP service, install
`crawlutron-rest`; to run it in a container, see the `crawlutron` image.

```python
from crawlutron.admission import AdmissionGate
```

The package ships `py.typed`: its annotations are part of the contract.

## License

MIT. See the repository's `LICENSE`.