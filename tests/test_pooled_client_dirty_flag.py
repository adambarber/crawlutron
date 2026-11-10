"""S5 contract: the pooled-client dirty flag tracks builds through the public door.

The harness's per-test autouse reset reads this flag so a test that never
touches the pooled client skips the loop-bound drain entirely; a test that
builds a client still drains on teardown. No magic comments, no issue numbers
in names.
"""

from crawlutron.transport import pooled_clients


def test_dirty_flag_tracks_pooled_client_builds_through_the_public_door() -> None:
    """``clients_were_built`` flips only when a client is actually built."""
    pooled = pooled_clients.http_singleton()
    pooled.reset_clients_built_flag()
    assert pooled.clients_were_built() is False

    pooled_clients.get_async_http_client()
    assert pooled.clients_were_built() is True

    pooled.reset_clients_built_flag()
    assert pooled.clients_were_built() is False
