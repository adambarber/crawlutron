"""How bytes move: the transport layer.

Every way this service retrieves a page body lives here -- the declared-crawler
and fingerprint-impersonating HTTP fetchers, the browser renderer and its pool,
the pinned transport that resolves and pins a host's addresses for the life of a
request, the request profile that shapes headers and credentials, and the body
decoding that turns wire bytes back into content.

It is the layer between the network and everything above it. What the bytes MEAN
is ``app.extraction``; whether the fetch was permitted is ``app.admission``;
caching a fetched body is here because the thing being cached is transport's own
output. A strategy takes a request and yields a classified result; deciding which
strategies to try, in what order, and how many times is ``app.orchestration``.
"""

from __future__ import annotations
