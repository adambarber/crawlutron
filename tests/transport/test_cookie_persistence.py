"""Cookie persistence - pure tests (no mocks)."""

from app.transport.cookie_jar import CookieJar
from tests._policy_factories import canonical


def test_cookie_jar_save_load_roundtrip(tmp_path):
    """CookieJar persists and restores cookies correctly."""
    path = str(tmp_path / "cookies.json")

    jar = CookieJar()
    jar.add_cookie_header(canonical("http://example.com/"), "session_id=abc123; Path=/")
    jar.add_cookie_header(canonical("http://example.com/"), "pref=dark; Path=/")
    jar.add_cookie_header(canonical("http://other.com/"), "token=xyz; Path=/")

    jar.export_to_file(path)

    # Load into new jar
    loaded = CookieJar.import_from_file(path)

    assert loaded.cookie_header_for(canonical("http://example.com/")) == "session_id=abc123; pref=dark"
    assert loaded.cookie_header_for(canonical("http://other.com/")) == "token=xyz"
    assert loaded.cookie_header_for(canonical("http://unknown.com/")) == ""


def test_cookie_jar_load_missing_file_returns_empty():
    """Loading from non-existent file returns empty jar."""
    jar = CookieJar.import_from_file("/nonexistent/path/cookies.json")
    assert jar.cookie_header_for(canonical("http://example.com/")) == ""


def test_cookie_jar_load_corrupted_file_returns_empty(tmp_path):
    """Loading from corrupted JSON returns empty jar (no crash)."""
    path = tmp_path / "corrupt.json"
    path.write_text("not valid json")
    jar = CookieJar.import_from_file(str(path))
    assert jar.cookie_header_for(canonical("http://example.com/")) == ""


def test_cookie_jar_persistence_preserves_multiple_domains(tmp_path):
    """Multiple domains are all preserved."""
    path = str(tmp_path / "cookies.json")
    jar = CookieJar()
    for i in range(5):
        jar.add_cookie_header(canonical(f"http://domain{i}.com/"), f"val={i}; Path=/")
    jar.export_to_file(path)
    loaded = CookieJar.import_from_file(path)
    for i in range(5):
        assert loaded.cookie_header_for(canonical(f"http://domain{i}.com/")) == f"val={i}"
