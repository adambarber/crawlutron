"""Attacker-controlled JSON-RPC fields cannot forge a line in MCP failure logs."""

from __future__ import annotations

import logging

import crawlutron_mcp.server as server_mod
from crawlutron.models.json_value import JsonValue
from crawlutron_mcp.server import handle_mcp_request
from tests.mcp.test_mcp import _META, _PROTOCOL_VERSION


def _raise_unhandled(*args, **kwargs):
    raise RuntimeError("unhandled")


async def test_malformed_method_cannot_forge_a_log_line(monkeypatch, caplog):
    """The logged method is escaped; the raw newline never reaches the sink."""
    monkeypatch.setattr(server_mod, "validate_meta", _raise_unhandled)
    forged = "tools/call\nINJECTED ERROR"
    body: dict[str, JsonValue] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": forged,
        "params": {"_meta": {**_META}},
    }
    with caplog.at_level(logging.ERROR, logger=server_mod.logger.name):
        status, _payload = await handle_mcp_request(
            body,
            protocol_version_header=_PROTOCOL_VERSION,
            method_header=forged,
            name_header=None,
            settings=None,
            shutdown_event=None,
        )
    assert status == 500
    records = [r for r in caplog.records if r.getMessage() == "mcp_request_failed"]
    assert len(records) == 1, caplog.text
    logged = records[0].__dict__["method"]
    assert "\n" not in logged and "\r" not in logged
    assert logged == "tools/call\\nINJECTED ERROR"


async def test_malformed_request_id_cannot_forge_a_log_line(monkeypatch, caplog):
    """A string id carrying a newline is escaped in the log extra."""
    monkeypatch.setattr(server_mod, "validate_meta", _raise_unhandled)
    forged_id = "7\r\nINJECTED ERROR"
    body: dict[str, JsonValue] = {
        "jsonrpc": "2.0",
        "id": forged_id,
        "method": "server/discover",
        "params": {"_meta": {**_META}},
    }
    with caplog.at_level(logging.ERROR, logger=server_mod.logger.name):
        status, _payload = await handle_mcp_request(
            body,
            protocol_version_header=_PROTOCOL_VERSION,
            method_header="server/discover",
            name_header=None,
            settings=None,
            shutdown_event=None,
        )
    assert status == 500
    records = [r for r in caplog.records if r.getMessage() == "mcp_request_failed"]
    assert len(records) == 1, caplog.text
    logged = records[0].__dict__["request_id"]
    assert "\n" not in logged and "\r" not in logged
    assert logged == "7\\r\\nINJECTED ERROR"
