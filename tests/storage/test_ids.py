"""Request/job id entropy contract (extended to handles).

The id is the only lookup credential for ``/v1/crawl/{id}``, so the
anti-brute-force policy must govern request handles too -- 12-hex was 48 bits.
"""

import re

from crawlutron.storage.ids import new_job_id, new_request_id

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def test_new_request_id_is_prefixed_full_entropy_hex():
    request_id = new_request_id()
    assert request_id.startswith("crawl_")
    assert HEX32.match(request_id.removeprefix("crawl_")), request_id


def test_new_job_id_is_full_entropy_hex():
    assert HEX32.match(new_job_id())


def test_ids_are_unique_across_mints():
    ids = {new_request_id() for _ in range(500)}
    ids |= {new_job_id() for _ in range(500)}
    assert len(ids) == 1000
