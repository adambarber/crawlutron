"""Request/job id entropy contract (extended to handles).

GET /v1/crawl/{request_id} takes the id as its only lookup credential, so
the "avoid brute-forceable short IDs" policy that job_store documents for
job_id must govern request handles too — the 12-hex mints were 48 bits of
entropy exposed in a status URL.
"""

import re

from app.storage.ids import new_job_id, new_request_id

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
