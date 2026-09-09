"""Adversarial fix (F-csv-injection): CSV cells must not execute as formulas.

Spreadsheet apps (Excel/Sheets/Numbers) execute cells whose first character
is ``=``, ``+``, ``-``, ``@``, TAB, or CR as a formula. Crawled page content
is attacker-influenced; a malicious page whose ``error`` field is
``=cmd|'/c calc'!A1`` would otherwise execute when the caller opens the
CSV output. The ``_csv_neutralize`` helper prefixes dangerous cells with a
single quote (which spreadsheets display-but-discard on render).
"""

from app.output_formats import (
    _csv_neutralize,
    serialize_results,
)


def test_csv_neutralize_quotes_equals_prefix():
    """= prefix is the most common formula trigger; must be quoted."""
    assert _csv_neutralize("=cmd|'/c calc'!A1", "error") == "'=cmd|'/c calc'!A1"


def test_csv_neutralize_handles_all_dangerous_prefixes():
    for prefix in ("+", "-", "@"):
        value = f"{prefix}payload"
        assert _csv_neutralize(value, "error") == f"'{value}"
    # TAB and CR are also dangerous; the literal-escape strings we pass.
    for prefix in ("\t", "\r"):
        value = f"{prefix}payload"
        assert _csv_neutralize(value, "error") == f"'{value}"


def test_csv_neutralize_passes_safe_values():
    """Plain text and non-prefixed values must be returned unchanged."""
    assert _csv_neutralize("plain text", "error") == "plain text"
    assert _csv_neutralize("404 not found", "error") == "404 not found"
    assert _csv_neutralize("see https://x/?q=test", "error") == "see https://x/?q=test"


def test_csv_neutralize_skips_non_neutralized_columns():
    """URL columns are returned unchanged so existing analyses still work."""
    assert _csv_neutralize("=foo", "original_url") == "=foo"
    assert _csv_neutralize("=foo", "normalized_url") == "=foo"
    assert _csv_neutralize("=foo", "final_url") == "=foo"


def test_csv_neutralize_handles_empty_value():
    assert _csv_neutralize("", "error") == ""


def test_serialize_results_csv_neutralizes_attacker_content():
    """End-to-end: an error field that starts with = is quoted in the CSV."""
    results = [{"original_url": "http://x/", "error": "=cmd|'/c calc'!A1"}]
    body, media_type = serialize_results(results, "csv")
    assert media_type == "text/csv; charset=utf-8"
    # The formula trigger must be preceded by a single quote.
    assert ",'=cmd" in body
    # And the raw unescaped formula must NOT appear (it would mean the
    # attacker payload was emitted unquoted).
    assert (
        "'" not in body.split("\n")[1].split(",")[1]
        or "=cmd" not in body.split("\n")[1].split(",")[1]
        or body.split("\n")[1].split(",")[1].startswith("'")
    )


def test_serialize_results_csv_preserves_url_columns():
    """URL columns are NOT neutralized: =foo in a URL is a query separator."""
    results = [{"original_url": "https://x/?q=foo&p=bar", "error": "ok"}]
    body, _ = serialize_results(results, "csv")
    assert "https://x/?q=foo&p=bar" in body
