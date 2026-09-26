"""#9 Cookie persistence - pure tests (no mocks)."""

import os
import tempfile

from app.crawl.strategies.cookie_jar import CookieJar


def test_cookie_jar_save_load_roundtrip():
    """CookieJar persists and restores cookies correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "cookies.json")

        jar = CookieJar()
        jar.add_cookie_header("http://example.com/", "session_id=abc123; Path=/")
        jar.add_cookie_header("http://example.com/", "pref=dark; Path=/")
        jar.add_cookie_header("http://other.com/", "token=xyz; Path=/")

        jar.export_to_file(path)

        # Load into new jar
        loaded = CookieJar.import_from_file(path)

        assert loaded.cookie_header_for("http://example.com/") == "session_id=abc123; pref=dark"
        assert loaded.cookie_header_for("http://other.com/") == "token=xyz"
        assert loaded.cookie_header_for("http://unknown.com/") == ""


def test_cookie_jar_load_missing_file_returns_empty():
    """Loading from non-existent file returns empty jar."""
    jar = CookieJar.import_from_file("/nonexistent/path/cookies.json")
    assert jar.cookie_header_for("http://example.com/") == ""


def test_cookie_jar_load_corrupted_file_returns_empty():
    """Loading from corrupted JSON returns empty jar (no crash)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "corrupt.json")
        with open(path, "w") as f:
            f.write("not valid json")
        jar = CookieJar.import_from_file(path)
        assert jar.cookie_header_for("http://example.com/") == ""


def test_cookie_jar_persistence_preserves_multiple_domains():
    """Multiple domains are all preserved."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "cookies.json")
        jar = CookieJar()
        for i in range(5):
            jar.add_cookie_header(f"http://domain{i}.com/", f"val={i}; Path=/")
        jar.export_to_file(path)
        loaded = CookieJar.import_from_file(path)
        for i in range(5):
            assert loaded.cookie_header_for(f"http://domain{i}.com/") == f"val={i}"
