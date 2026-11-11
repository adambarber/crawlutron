"""The admin audit trail: every key-admin action gets one durable, structured row.

Tests through public doors: an admin action at the keys door, then the audit
read back through the store's public list door. Red-first per chokepoint: a
mutant that drops the audit write at one chokepoint reds exactly its test.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from crawlutron.api import caller_auth
from crawlutron.storage.admin_audit_store import AdminAuditStore
from crawlutron.storage.caller_key_store import CallerKeyStore
from tests._async_harnesses import serve
from tests._quota_harness import TEST_ADMIN_TOKEN, stores
from tests.factories.client import isolation_app


def _audit(tmp_path):
    return AdminAuditStore(tmp_path / "audit.db")


def _app(tmp_path):
    store, results, keys = stores(tmp_path)
    audit = _audit(tmp_path)
    keys.attach_audit(audit)
    app = isolation_app(store, results, keys)
    app.state.audit_store = audit
    return app, audit


async def test_a_minted_key_lands_one_mint_event(tmp_path):
    """Mint at the keys door; the audit row records actor, action, and target id."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        admin = {"X-API-Key": TEST_ADMIN_TOKEN}
        issued = await client.post("/v1/keys", json={"label": "mint-one"}, headers=admin)
        assert issued.status_code == 201, issued.text

        events = audit.list_events()
        assert len(events) == 1
        event = events[0]
        assert event.action == "key_mint"
        assert event.target_key_id == issued.json()["key_id"]
        assert event.actor == "admin"
        assert event.outcome == "granted"
        # Structured fields only: no secret material rides the row.
        assert issued.json()["secret"] not in str(event.model_dump())


async def test_a_revoked_key_lands_one_revoke_event(tmp_path):
    """Revoke at the keys door; the audit row names the revoked key id."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        creds = {"X-API-Key": TEST_ADMIN_TOKEN}
        issued = await client.post("/v1/keys", json={"label": "revoke-target"}, headers=creds)
        key_id = issued.json()["key_id"]
        revoked = await client.delete(f"/v1/keys/{key_id}", headers=creds)
        assert revoked.status_code == 200, revoked.text

        events = audit.list_events()
        assert [event.action for event in events] == ["key_revoke", "key_mint"]
        assert events[0].target_key_id == key_id


async def test_a_key_list_access_lands_one_list_event(tmp_path):
    """The keys inventory read is itself audited: the reader's action is recorded."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        headers = {"X-API-Key": TEST_ADMIN_TOKEN}
        await client.post("/v1/keys", json={"label": "list-reader"}, headers=headers)
        listed = await client.get("/v1/keys", headers=headers)
        assert listed.status_code == 200, listed.text

        events = audit.list_events()
        assert [event.action for event in events] == ["key_list", "key_mint"]
        assert events[0].actor == "admin"


async def test_a_failed_operator_auth_lands_one_durable_event(tmp_path):
    """A failed admin-token guess is recorded durably, not only counted (#696)."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        before = audit.list_events()
        response = await client.get("/v1/keys", headers={"X-API-Key": "wrong-token"})
        assert response.status_code == 401

        events = audit.list_events()
        assert len(events) == len(before) + 1
        event = events[0]
        assert event.action == "operator_auth_failure"
        assert event.actor == "anonymous"
        assert event.outcome == "refused"
        assert event.target_key_id is None


async def test_audit_rows_carry_no_free_text_and_no_secret_material(tmp_path):
    """The row schema is structured fields only; the presented secret never lands."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        await client.get("/v1/keys", headers={"X-API-Key": "wrong-token"})
        secret_holder = {"X-API-Key": TEST_ADMIN_TOKEN}
        issued = await client.post("/v1/keys", json={"label": "no-secret"}, headers=secret_holder)

        with closing(sqlite3.connect(tmp_path / "audit.db")) as conn:
            rows = conn.execute("SELECT * FROM key_admin_events").fetchall()
        blob = str(rows)
        assert "wrong-token" not in blob
        assert issued.json()["secret"] not in blob
        assert TEST_ADMIN_TOKEN not in blob


async def test_audit_events_are_numbered_by_monotonic_sequence(tmp_path):
    """Rows append in order and read newest-first with a strictly falling sequence."""
    app, audit = _app(tmp_path)
    async with serve(app) as client:
        auth = {"X-API-Key": TEST_ADMIN_TOKEN}
        await client.post("/v1/keys", json={"label": "seq-one"}, headers=auth)
        await client.post("/v1/keys", json={"label": "seq-two"}, headers=auth)

        events = audit.list_events()
        sequences = [event.sequence for event in events]
        assert sequences == sorted(sequences, reverse=True)
        assert len(set(sequences)) == len(sequences)


async def test_retention_sweep_drops_lapsed_rows_at_the_roll_point(tmp_path):
    """The append rolls retention: rows beyond the bound or older than the window
    are dropped at the next append, never on the read path."""
    audit = _audit(tmp_path)
    audit.record_event(actor="admin", action="key_mint", target_key_id="key_old", outcome="granted")
    # Backdate the row past the retention window directly at the stored table.
    with closing(sqlite3.connect(tmp_path / "audit.db")) as conn:
        conn.execute("UPDATE key_admin_events SET recorded_at = 0.0")
        conn.commit()

    audit.record_event(actor="admin", action="key_mint", target_key_id="key_new", outcome="granted")

    assert [event.target_key_id for event in audit.list_events()] == ["key_new"]


async def test_the_event_cap_bounds_the_trail_under_a_mint_flood(tmp_path):
    """Worst-case ledger is the capacity cap: the oldest rows leave at the roll."""
    audit = AdminAuditStore(tmp_path / "audit.db", max_events=5)
    for index in range(8):
        audit.record_event(actor="admin", action="key_mint", target_key_id=f"key_{index}", outcome="granted")

    events = audit.list_events()
    assert len(events) == 5
    assert events[0].target_key_id == "key_7"


async def test_async_twin_round_trip_off_the_loop(tmp_path):
    """The atwin twins run the same writes off the loop, in order."""
    audit = _audit(tmp_path)
    await audit.arecord_event(actor="admin", action="key_mint", target_key_id="key_a", outcome="granted")

    events = await audit.alist_events()
    assert [(event.action, event.target_key_id) for event in events] == [("key_mint", "key_a")]


async def test_the_store_method_writes_flow_through_the_wired_audit_store(tmp_path):
    """The audit write site is the store method itself: any caller of issue/revoke
    gets the durable row, not just the HTTP door."""
    keys = CallerKeyStore(tmp_path / "keys.db")
    audit = _audit(tmp_path)
    keys.attach_audit(audit)

    issued = keys.issue_key("agent-a")
    keys.revoke_key(issued.key_id)

    actions = [event.action for event in audit.list_events()]
    assert actions == ["key_revoke", "key_mint"]


def test_the_401_raiser_records_the_audit_event(tmp_path):
    """The auth failure records through the store, one site, from the raiser."""
    audit = _audit(tmp_path)
    caller_auth.record_operator_auth_failure(audit, actor="anonymous")

    assert [event.action for event in audit.list_events()] == ["operator_auth_failure"]
