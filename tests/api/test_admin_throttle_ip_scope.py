"""The admin key-verify ladder, scoped per remote IP (#1123).

A stranger's failed guesses pace their own door and only their own: the
operator's /v1/keys stays answerable from every other address.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest

from crawlutron.service.errors import KeyVerifyThrottled
from crawlutron_rest.api.throttle import Throttle
from tests._clocks import ClockState, arrange_clock
from tests._quota_harness import TEST_ADMIN_TOKEN, stores
from tests.factories.client import close_all, isolation_app

_ATTACKER = "198.51.100.7"
_OPERATOR = "203.0.113.20"


def _arranged() -> tuple[ClockState, Throttle]:
    """A Throttle over an arrange clock the test advances."""
    state, clock = arrange_clock()
    return state, Throttle(clock=clock)


def _arm(throttle: Throttle, remote: str | None, count: int) -> None:
    """Record ``count`` admin failures from one address."""
    for _ in range(count):
        throttle.record_admin_failure(remote)


def test_a_cold_admin_door_admits_every_address():
    """Happy path: with the ladder cold, both addresses pass the admin door."""
    _state, throttle = _arranged()
    throttle.check_admin(_ATTACKER)
    throttle.check_admin(_OPERATOR)


def test_failures_from_one_address_cool_only_that_address():
    """Boundary: one short of the cap still admits; the cap-crossing failure
    refuses the guessing address while another address passes untouched."""
    _state, throttle = _arranged()
    _arm(throttle, _ATTACKER, Throttle.MAX_FAILURES - 1)
    throttle.check_admin(_ATTACKER)
    throttle.record_admin_failure(_ATTACKER)
    with pytest.raises(KeyVerifyThrottled) as raised:
        throttle.check_admin(_ATTACKER)
    assert raised.value.status == 429
    assert raised.value.code == "key_verify_throttled"
    throttle.check_admin(_OPERATOR)


def test_a_stranger_cannot_lock_the_operator_out():
    """Malicious actor: a renewable-forever flood from one address paces that
    address alone; the operator's address never sees the ladder move."""
    _state, throttle = _arranged()
    for _ in range(Throttle.MAX_FAILURES * 4):
        throttle.record_admin_failure(_ATTACKER)
    with pytest.raises(KeyVerifyThrottled):
        throttle.check_admin(_ATTACKER)
    throttle.check_admin(_OPERATOR)


def test_failures_split_across_two_addresses_trip_neither():
    """Boundary: 4 failures from one address and 1 from another leave both
    addresses admitted -- the cap is per address, not per process."""
    _state, throttle = _arranged()
    _arm(throttle, _ATTACKER, Throttle.MAX_FAILURES - 1)
    throttle.record_admin_failure(_OPERATOR)
    throttle.check_admin(_ATTACKER)
    throttle.check_admin(_OPERATOR)


def test_a_success_clears_only_the_presenting_address():
    """The success arm is scoped: the presenting address starts a fresh bar
    while another address's armed ladder stays armed."""
    _state, throttle = _arranged()
    for _ in range(Throttle.MAX_FAILURES):
        throttle.record_admin_failure(_OPERATOR)
    for _ in range(Throttle.MAX_FAILURES - 1):
        throttle.record_admin_failure(_ATTACKER)
    throttle.record_admin_success(_ATTACKER)
    throttle.record_admin_failure(_ATTACKER)
    throttle.check_admin(_ATTACKER)
    with pytest.raises(KeyVerifyThrottled):
        throttle.check_admin(_OPERATOR)


def test_a_clientless_request_pools_into_one_fixed_bucket():
    """A transport providing no remote address pools into one bucket -- the
    anonymous ladder's clientless law; a named address is its own bucket."""
    _state, throttle = _arranged()
    for _ in range(Throttle.MAX_FAILURES):
        throttle.record_admin_failure(None)
    with pytest.raises(KeyVerifyThrottled):
        throttle.check_admin(None)
    throttle.check_admin(_ATTACKER)


def test_a_lapsed_cooldown_restores_only_that_address():
    """The cooldown expires per address: once lapsed, the cooled address is
    admitted again without touching any other address."""
    state, throttle = _arranged()
    for _ in range(Throttle.MAX_FAILURES):
        throttle.record_admin_failure(_ATTACKER)
    with pytest.raises(KeyVerifyThrottled):
        throttle.check_admin(_ATTACKER)
    state["now"] += 61.0
    throttle.check_admin(_ATTACKER)


def test_the_shared_store_keys_the_admin_ladder_per_address(tmp_path):
    """The coordination store takes the scoped key unchanged: a second
    Throttle over one db observes the cooled address and no other."""
    db = tmp_path / "crawler.db"
    first = Throttle(shared_db_path=db)
    for _ in range(Throttle.MAX_FAILURES):
        first.record_admin_failure(_ATTACKER)
    second = Throttle(shared_db_path=db)
    with pytest.raises(KeyVerifyThrottled):
        second.check_admin(_ATTACKER)
    second.check_admin(_OPERATOR)


def test_a_forgotten_shared_store_is_closed_by_the_registry_reset(tmp_path):
    """Production owns the shared-store connection: a Throttle that binds one
    and never unbinds it leaks nothing, because the registry reset closes it."""
    from crawlutron.runtime import process_state
    from tests.invariants.test_no_leaked_store_connections import live_ledgered_sites

    db = tmp_path / "crawler.db"
    before = len(live_ledgered_sites())
    forgotten = Throttle(shared_db_path=db)
    forgotten.record_admin_failure(_ATTACKER)
    assert forgotten.shared is True
    assert len(live_ledgered_sites()) > before
    process_state.reset_all_process_state()
    assert forgotten.shared is False
    assert len(live_ledgered_sites()) == before


async def test_the_async_twins_carry_the_scope():
    """The doors await the async twins; the scope rides the twin's arguments."""
    _state, throttle = _arranged()
    for _ in range(Throttle.MAX_FAILURES):
        await throttle.arecord_admin_failure(_ATTACKER)
    with pytest.raises(KeyVerifyThrottled):
        await throttle.acheck_admin(_ATTACKER)
    await throttle.acheck_admin(_OPERATOR)


@asynccontextmanager
async def _two_address_clients(
    tmp_path,
) -> AsyncGenerator[tuple[httpx.AsyncClient, httpx.AsyncClient], None]:
    """One isolated keys-door app served to two clients, each presenting a
    different remote address: the attacker's and the operator's."""
    store, results, keys = stores(tmp_path)
    app = isolation_app(store, results, keys)
    attacker = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(_ATTACKER, 40010)), base_url="http://test"
    )
    operator = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(_OPERATOR, 40020)), base_url="http://test"
    )
    try:
        yield attacker, operator
    finally:
        await attacker.aclose()
        await operator.aclose()
        close_all(store, results, keys)


async def _mint_as_operator(client: httpx.AsyncClient) -> httpx.Response:
    """The operator mints one key through the public keys door."""
    return await client.post(
        "/v1/keys", json={"label": "operator-a"}, headers={"X-API-Key": TEST_ADMIN_TOKEN}
    )


async def test_five_failed_admin_guesses_from_one_address_do_not_block_another(tmp_path):
    """The issue's red line: five junk Authorization headers lock the
    attacker's own door; the operator still mints a key."""
    async with _two_address_clients(tmp_path) as (attacker, operator):
        for i in range(Throttle.MAX_FAILURES):
            refused = await attacker.get("/v1/keys", headers={"Authorization": f"Bearer junk-{i}"})
            assert refused.status_code == 401, refused.text
        locked = await attacker.get("/v1/keys", headers={"Authorization": "Bearer junk-999"})
        assert locked.status_code == 429, locked.text
        assert locked.json()["code"] == "key_verify_throttled"
        minted = await _mint_as_operator(operator)
        assert minted.status_code == 201, minted.text


async def test_keyed_door_guesses_do_not_arm_the_admin_ladder(tmp_path):
    """Only admin-door attempts count: junk caller-key guesses on the crawl
    door pace the keyed ladders, never the operator's admin ladder."""
    async with _two_address_clients(tmp_path) as (attacker, operator):
        for i in range(Throttle.MAX_FAILURES):
            refused = await attacker.post(
                "/v1/crawl",
                headers={"X-API-Key": f"ck_0123456789abcdef.junk-{i}"},
                json={"url": "http://example.com/", "async_mode": True},
            )
            assert refused.status_code == 401, refused.text
        guess = await attacker.get("/v1/keys", headers={"Authorization": "Bearer junk-999"})
        assert guess.status_code == 401, guess.text
        minted = await _mint_as_operator(operator)
        assert minted.status_code == 201, minted.text
